"""Phase 2 acceptance tests (STORY-015): background processing, isolation, retries, retention, saved work.

The web app, background worker, job store and MCP tools are the real ones;
only the two AI calls (transcription and drafting) are replaced by fakes.
"""

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from starlette.testclient import TestClient

import ai_services
import app_core
import attestation
import audit_store
import auth_sessions
import clinician_registry
import encounter_files
import job_store
import note_drafting
import server
import ui_bridge
from bridge_auth import ToolCallError
from encounter_routes import purge_expired_audio, queue_status
from encounter_worker import EncounterWorker

PASSWORD, PIN = "correct horse battery", "246810"
ROOT = Path(__file__).resolve().parent.parent
AUDIO = b"RIFF....WAVEfmt fake-audio-bytes-for-a-test-recording"
PATIENTS = {"priya": ("epic", "epic-pt-55219"), "grace": ("epic", "epic-pt-10293847")}


async def via_mcp(name, args):
    """What the web app does: call the tool over MCP (with full input validation)."""
    try:
        result = await server.mcp.call_tool(name, args)
    except (ToolError, ResourceNotFoundError) as exc:
        raise ToolCallError(400, str(exc)) from exc
    payload = result.structured_content
    if result.is_error:
        raise ToolCallError(400, result.content[0].text if result.content else "tool failed")
    return payload


async def fake_transcribe(audio, filename, content_type):
    return "Clinician: How long have you been tired? Patient: Six weeks, and very thirsty."


def _draft_for(transcript):
    who = "Grace" if "Grace" in transcript else "Priya"
    return ai_services.draft_to_dict(note_drafting.parse_draft(json.dumps({
        "note_text": f"{who}: six weeks of fatigue and thirst.", "confidence": 0.85, "confidence_reason": "Clear.",
        "codes": [{"code": "R53.83", "code_system": "icd-10", "description": "Other fatigue"}],
        "codes_confidence": 0.8, "codes_confidence_reason": "Symptom codes.",
        "care_gaps": [{"description": "Overdue for screening mammogram", "confidence": 0.9, "confidence_reason": ""}],
        "test_orders": [{"test_name": "Hemoglobin A1c", "reason": "Confirm suspected diabetes"}],
        "referral": {"needed": False, "specialty": "", "reason": ""},
    })))


async def fake_draft(transcript):
    return _draft_for(transcript)


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def env(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(app_core, "AUDIT_LOG_PATH", tmp_path / "audit_log.jsonl")
    monkeypatch.setenv("MESHMEDIC_ATTESTATION_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("MESHMEDIC_CLINICIANS_FILE", str(tmp_path / "clinicians.json"))
    monkeypatch.setenv("MESHMEDIC_ENCOUNTER_DATA_DIR", str(tmp_path / "encounters"))
    clinician_registry.add_clinician("dr.rivera", "Dr. A. Rivera", PASSWORD, PIN)
    clinician_registry.add_clinician("dr.chen", "Dr. B. Chen", PASSWORD, PIN)
    store = job_store.SqliteJobStore(tmp_path / "jobs.sqlite3", clock=lambda: clock[0])
    monkeypatch.setattr(ui_bridge, "invoke_tool", via_mcp)
    monkeypatch.setattr(ui_bridge.auth, "_call_tool", via_mcp)
    monkeypatch.setattr(ui_bridge.auth, "state", auth_sessions.AuthState())
    monkeypatch.setattr(ui_bridge.encounter_api, "_call_tool", via_mcp)
    monkeypatch.setattr(ui_bridge.encounter_api, "store", store)
    yield store
    store.close()


def worker(store, name="w1", transcribe=fake_transcribe, draft=fake_draft, call_tool=via_mcp):
    return EncounterWorker(store, call_tool, transcribe, draft, name, logging.getLogger("test"), ToolCallError)


def client(clinician="dr.rivera"):
    c = TestClient(ui_bridge.app)
    r = c.post("/api/auth/login", json={"clinician_id": clinician, "password": PASSWORD})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def start(c, patient="priya", key=None):
    ehr, pid = PATIENTS[patient]
    r = c.post("/api/encounters", json={"ehr_system": ehr, "patient_id": pid, "idempotency_key": key or f"key-{patient}-1"})
    assert r.status_code == 200, r.text
    return r.json()["encounter_id"]


def capture(c, enc, transcript=None):
    if transcript:
        assert c.post(f"/api/encounters/{enc}/transcript", json={"text": transcript}).status_code == 200
    else:
        r = c.post(f"/api/encounters/{enc}/audio", files={"audio": ("clip.webm", AUDIO, "audio/webm")})
        assert r.status_code == 200, r.text
    r = c.post(f"/api/encounters/{enc}/end")
    assert r.status_code == 202 and r.json()["status"] == "processing"


def _entries():
    return audit_store.read_entries(app_core.AUDIT_LOG_PATH)


def _count(action):
    return sum(1 for e in _entries() if e["action"] == action)


def _queue(c):
    return {i["encounter_id"]: i for i in c.get("/api/queue").json()["items"]}


# ---------- 2.1 end one consultation, start the next, first becomes ready on its own ----------


def test_end_consultation_and_move_straight_to_the_next_patient(env):
    c = client()
    first = start(c, "priya")
    capture(c, first)
    second = start(c, "grace")  # no waiting on the first
    assert _queue(c)[first]["queue_status"] == "processing"
    assert _queue(c)[second]["queue_status"] == "capturing"
    assert asyncio.run(worker(env).run_once())
    item = _queue(c)[first]
    assert item["queue_status"] == "ready_for_review" and item["note_status"] == "ai_suggested"
    bundle = c.get(f"/api/encounters/{first}").json()
    assert bundle["note"]["text"].startswith("Priya") and bundle["code_set"]["codes"][0]["code"] == "R53.83"
    assert {s["kind"] for s in bundle["suggestions"]} == {"care_gap", "test_order"}


# ---------- 2.2 two patients at once never mix ----------


def test_two_encounters_processed_at_once_never_share_content(env):
    c = client()
    a, b = start(c, "priya"), start(c, "grace")
    capture(c, a, "Priya says she has been tired for six weeks.")
    capture(c, b, "Grace says her blood pressure has been high.")
    async def both():
        return await asyncio.gather(worker(env, "w1").run_once(), worker(env, "w2").run_once())
    assert asyncio.run(both()) == [True, True]
    ba, bb = c.get(f"/api/encounters/{a}").json(), c.get(f"/api/encounters/{b}").json()
    assert ba["encounter"]["patient_id"] == "epic-pt-55219" and ba["note"]["text"].startswith("Priya")
    assert bb["encounter"]["patient_id"] == "epic-pt-10293847" and bb["note"]["text"].startswith("Grace")
    notes = [e for e in _entries() if e["action"] == "generate_note"]
    assert {(n["encounter_id"], n["patient_id"]) for n in notes} == {(a, "epic-pt-55219"), (b, "epic-pt-10293847")}


def test_a_note_for_another_patients_encounter_is_refused(env):
    enc = asyncio.run(via_mcp("start_encounter", {"ehr_system": "epic", "patient_id": "epic-pt-55219",
                                                   "idempotency_key": "k-mismatch-1"}))["encounter_id"]
    with pytest.raises(ToolCallError, match="different patient"):
        asyncio.run(via_mcp("generate_encounter_note", {
            "ehr_system": "epic", "patient_id": "epic-pt-10293847", "transcript": "t", "note_text": "n",
            "confidence": 0.9, "encounter_id": enc}))


# ---------- 2.3 stop after each stage, restart: exactly one of everything ----------


class Crash(BaseException):
    """Simulates the process dying (not an Exception, so the worker cannot catch it)."""


@pytest.mark.parametrize("crash_in", ["transcribe", "draft", "record", "check", "between_tools"])
def test_crash_after_any_stage_resumes_with_exactly_one_note(env, clock, crash_in):
    c = client()
    enc = start(c)
    capture(c, enc)
    w = worker(env)
    calls = {"n": 0}

    async def crashing_call_tool(name, args):
        result = await via_mcp(name, args)
        if crash_in == "between_tools" and name == "generate_encounter_note":
            raise Crash
        return result

    if crash_in == "between_tools":
        w = worker(env, call_tool=crashing_call_tool)
    else:
        original = getattr(w, f"_stage_{crash_in}")

        async def crash_after(enc_id):
            await original(enc_id)
            calls["n"] += 1
            raise Crash
        setattr(w, f"_stage_{crash_in}", crash_after)

    with pytest.raises(Crash):
        asyncio.run(w.run_once())
    assert env.get_for_encounter(enc).status == "running"  # the dead worker still holds the lease
    clock[0] += job_store.LEASE_SECONDS + 1  # lease expires
    assert asyncio.run(worker(env, "w2").run_once())
    assert env.get_for_encounter(enc).status == "ready"
    assert (_count("generate_note"), _count("suggest_codes"), _count("record_ai_suggestions")) == (1, 1, 1)


def test_ending_twice_queues_one_job(env):
    c = client()
    enc = start(c)
    capture(c, enc)
    assert c.post(f"/api/encounters/{enc}/end").status_code == 202
    assert len(env.all_jobs()) == 1


# ---------- 2.4 failures: retries, then needs attention, audio kept, no partial note ----------


def test_transient_failures_retry_three_times_then_need_attention(env, clock):
    c = client()
    enc = start(c)
    capture(c, enc)

    async def always_rate_limited(audio, filename, content_type):
        raise ai_services.TransientAIError("Rate limited by the OpenAI API. Try again shortly.")

    w = worker(env, transcribe=always_rate_limited)
    statuses = []
    for _ in range(job_store.MAX_ATTEMPTS):
        assert asyncio.run(w.run_once())
        statuses.append(env.get_for_encounter(enc).status)
        clock[0] += 300
    assert statuses == ["retry_wait", "retry_wait", "needs_attention"]
    item = _queue(c)[enc]
    assert item["queue_status"] == "needs_attention" and item["attempts"] == 3
    assert encounter_files.exists(enc, "audio") and _count("generate_note") == 0
    # Retry from the queue, now that the service is back.
    assert c.post(f"/api/encounters/{enc}/retry").json()["status"] == "processing"
    assert asyncio.run(worker(env).run_once())
    assert _queue(c)[enc]["queue_status"] == "ready_for_review"


def test_malformed_ai_output_is_retried_and_leaves_no_partial_records(env):
    c = client()
    enc = start(c)
    capture(c, enc)

    async def malformed(transcript):
        raise ai_services.TransientAIError("The AI draft was malformed (draft was not valid JSON). Try again.")

    asyncio.run(worker(env, draft=malformed).run_once())
    assert env.get_for_encounter(enc).status == "retry_wait"
    assert _count("generate_note") == _count("suggest_codes") == _count("record_ai_suggestions") == 0
    assert not encounter_files.exists(enc, "draft")


def test_a_permanent_failure_needs_attention_immediately(env):
    c = client()
    enc = start(c)
    capture(c, enc)

    async def refused(transcript):
        raise ai_services.PermanentAIError("The AI declined to draft this note. Write it yourself instead.")

    asyncio.run(worker(env, draft=refused).run_once())
    job = env.get_for_encounter(enc)
    assert (job.status, job.attempts, job.error_class) == ("needs_attention", 1, "UpstreamRejected")


# ---------- 2.5 the job store holds no patient data ----------


def test_job_store_rows_contain_no_patient_data(env):
    c = client()
    enc = start(c)
    capture(c, enc)
    asyncio.run(worker(env).run_once())
    stored = json.dumps(env.raw_rows())
    for forbidden in ("Priya", "Natarajan", "E55219", "1975-07-22", "epic-pt-55219", "fatigue", "thirsty"):
        assert forbidden.lower() not in stored.lower(), forbidden


# ---------- 2.6 audio encrypted, owner-only ----------


def test_audio_is_encrypted_on_disk_and_only_its_clinician_can_play_it(env):
    c = client("dr.rivera")
    enc = start(c)
    capture(c, enc)
    on_disk = (encounter_files.data_root() / enc / "audio.enc").read_bytes()
    assert b"fake-audio-bytes" not in on_disk
    assert c.get(f"/api/encounters/{enc}/audio").content == AUDIO
    assert TestClient(ui_bridge.app).get(f"/api/encounters/{enc}/audio").status_code == 401
    assert client("dr.chen").get(f"/api/encounters/{enc}/audio").status_code == 403
    assert client("dr.chen").get(f"/api/encounters/{enc}").status_code == 403


def test_an_encrypted_file_moved_to_another_encounter_will_not_decrypt(env):
    c = client()
    a, b = start(c, "priya"), start(c, "grace")
    capture(c, a)
    (encounter_files.data_root() / b).mkdir(parents=True, exist_ok=True)
    shutil.copy(encounter_files.data_root() / a / "audio.enc", encounter_files.data_root() / b / "audio.enc")
    with pytest.raises(encounter_files.EncounterFileError, match="authenticated decryption"):
        encounter_files.read(b, "audio")


# ---------- 2.7 retention deletes audio, logs it, never touches the note ----------


def _sign(c, enc):
    bundle = c.get(f"/api/encounters/{enc}").json()
    note_id = bundle["note"]["note_id"]
    assert c.post("/api/tools/approve_encounter_note", json={"note_id": note_id}).status_code == 200
    assert c.post("/api/auth/reauth", json={"pin": PIN}).status_code == 200
    r = c.post(f"/api/notes/{note_id}/sign", json={"content_hash": attestation.content_hash(bundle["note"]["text"])})
    assert r.status_code == 200, r.text
    return note_id


def test_expired_audio_is_deleted_and_logged_and_the_note_is_unchanged(env):
    c = client()
    signed_enc, unsigned_enc = start(c, "priya"), start(c, "grace")
    capture(c, signed_enc)
    capture(c, unsigned_enc)
    asyncio.run(worker(env).run_once())
    asyncio.run(worker(env).run_once())
    _sign(c, signed_enc)
    before = c.get(f"/api/encounters/{signed_enc}").json()["note"]
    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    for _ in range(2):  # a second sweep must not log a second deletion
        asyncio.run(purge_expired_audio(via_mcp, ui_bridge.auth, 0, "synthetic-prototype", logging.getLogger("t"), now=later))
    assert not encounter_files.exists(signed_enc, "audio")
    assert encounter_files.exists(unsigned_enc, "audio")  # not signed yet: kept
    deletions = [e for e in _entries() if e["action"] == "audio_deleted"]
    assert len(deletions) == 1 and deletions[0]["encounter_id"] == signed_enc
    assert deletions[0]["actor"]["type"] == "system" and deletions[0]["retention_days"] == 0
    after = c.get(f"/api/encounters/{signed_enc}").json()["note"]
    assert (after["text"], after["status"]) == (before["text"], "signed")


def test_audio_is_kept_until_the_retention_period_ends(env):
    c = client()
    enc = start(c)
    capture(c, enc)
    asyncio.run(worker(env).run_once())
    _sign(c, enc)
    asyncio.run(purge_expired_audio(via_mcp, ui_bridge.auth, 7, "p", logging.getLogger("t")))
    assert encounter_files.exists(enc, "audio") and _count("audio_deleted") == 0


def test_an_ai_client_cannot_record_an_audio_deletion(env):
    with pytest.raises(ToolCallError, match="refused"):
        asyncio.run(via_mcp("record_audio_deletion", {"encounter_id": "enc_" + "0" * 32, "retention_days": 0,
                                                      "policy_ref": "p", "attestation_token": "forged.token"}))


# ---------- 2.8 the background job never makes a clinical decision ----------


def test_background_processing_only_prepares_and_compliance_passes(env):
    c = client()
    enc = start(c)
    capture(c, enc)
    asyncio.run(worker(env).run_once())
    actors = {e["action"]: e["actor"]["type"] for e in _entries()
              if e["action"] in ("generate_note", "suggest_codes", "record_ai_suggestions")}
    assert actors == {"generate_note": "ai", "suggest_codes": "ai", "record_ai_suggestions": "ai"}
    for forbidden in ("approve_note", "sign_note", "order_test", "approve_codes", "identify_care_gap"):
        assert _count(forbidden) == 0
    assert server.run_compliance_check().all_passed


# ---------- 2.9 saved work survives a reload and shows the same elsewhere ----------


def test_edits_survive_reload_and_show_in_a_second_browser(env):
    c = client()
    enc = start(c)
    capture(c, enc)
    asyncio.run(worker(env).run_once())
    note_id = c.get(f"/api/encounters/{enc}").json()["note"]["note_id"]
    edited = "Priya: six weeks of fatigue, thirst and nocturia. Edited by clinician."
    assert c.post("/api/tools/edit_encounter_note", json={"note_id": note_id, "edited_note_text": edited}).status_code == 200
    second_browser = client()  # same clinician, new session, nothing in browser memory
    reopened = second_browser.get(f"/api/encounters/{enc}").json()
    assert reopened["note"]["text"] == edited and reopened["note"]["status"] == "in_review"
    views = [e for e in _entries() if e["action"] == "view_encounter"]
    assert views and all(v["actor"]["clinician_id"] == "dr.rivera" for v in views)


# ---------- 2.10 SQLite stays behind one module ----------


def test_only_the_job_store_module_uses_sqlite():
    users = [p.name for p in ROOT.glob("*.py")
             if re.search(r"import sqlite3|sqlite3[.][a-z]", p.read_text(encoding="utf-8"))]
    assert users == ["job_store.py"]


# ---------- queue status vocabulary ----------


def test_queue_status_combines_progress_and_review():
    def job(status):
        return job_store.Job("j", "enc", "check", status, 0, 0, None, 0, 0)
    assert queue_status(None, None) == "capturing"
    assert queue_status(job("retry_wait"), None) == "processing"
    assert queue_status(job("needs_attention"), None) == "needs_attention"
    assert queue_status(job("ready"), "ai_suggested") == "ready_for_review"
    assert queue_status(job("ready"), "clinician_reviewed") == "awaiting_signature"
    assert queue_status(job("ready"), "signed") == "signed"
