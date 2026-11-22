"""Phase 1 acceptance tests (STORY-014): clinician identity, authorization and note sign-off."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from starlette.testclient import TestClient

import app_core
import attestation
import audit_store
import auth_sessions
import clinician_registry
import item_status
import protected_paths
import server
import ui_bridge
from bridge_auth import ToolCallError
from tools_note_signoff import AuthEvent

PASSWORD, PIN = "correct horse battery", "246810"
SNAPSHOT = json.loads((Path(__file__).parent / "tool_contract_snapshot.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_core, "AUDIT_LOG_PATH", tmp_path / "audit_log.jsonl")
    monkeypatch.setenv("MESHMEDIC_ATTESTATION_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("MESHMEDIC_CLINICIANS_FILE", str(tmp_path / "clinicians.json"))
    clinician_registry.add_clinician("dr.rivera", "Dr. A. Rivera", PASSWORD, PIN)
    clinician_registry.add_clinician("dr.chen", "Dr. B. Chen", PASSWORD, PIN)


def _entries():
    path = app_core.AUDIT_LOG_PATH
    return audit_store.read_entries(path)


def _key():
    return attestation.load_or_create_private_key()


def _reviewed_note(text="Patient reports six weeks of fatigue."):
    note = server.generate_encounter_note(
        ehr_system="epic", patient_id="epic-pt-55219", transcript="Fatigue for six weeks.",
        note_text=text, confidence=0.9,
    )
    server.approve_encounter_note(note_id=note.note_id)
    return note


def _sign_token(note_id, text, clinician="dr.rivera", now=None, action=attestation.SIGN_NOTE):
    return attestation.issue(_key(), clinician, "Dr. A. Rivera", action, note_id, attestation.content_hash(text), now=now)


# ---------- 1.1 no attestation, no signature -- including an AI client ----------


@pytest.mark.parametrize("token", ["not-an-attestation", "a.b", "x" * 50])
def test_signing_without_a_valid_attestation_is_refused_and_writes_nothing(token):
    note = _reviewed_note()
    before = _entries()
    with pytest.raises(ToolError, match="Only a signed-in clinician"):
        server.sign_encounter_note(note_id=note.note_id, content_hash=attestation.content_hash(note.note_text),
                                   attestation_token=token)
    assert _entries() == before


def test_ai_client_calling_the_sign_tool_over_mcp_is_refused():
    note = _reviewed_note()
    before = _entries()
    args = {"note_id": note.note_id, "content_hash": attestation.content_hash(note.note_text),
            "attestation_token": "forged.token"}
    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool("sign_encounter_note", args))
    assert _entries() == before


def test_attestation_signed_with_another_key_is_refused(tmp_path):
    note = _reviewed_note()
    _key()  # the real key pair exists
    rogue = attestation.load_or_create_private_key(tmp_path / "rogue")
    token = attestation.issue(rogue, "dr.rivera", "Dr. A. Rivera", attestation.SIGN_NOTE, note.note_id,
                              attestation.content_hash(note.note_text))
    with pytest.raises(ToolError, match="signature is invalid"):
        server.sign_encounter_note(note_id=note.note_id, content_hash=attestation.content_hash(note.note_text),
                                   attestation_token=token)


def test_no_public_key_means_nothing_can_be_signed(monkeypatch, tmp_path):
    note = _reviewed_note()
    token = _sign_token(note.note_id, note.note_text)
    (attestation.key_dir() / attestation.PUBLIC_KEY_NAME).unlink()
    with pytest.raises(ToolError, match="not configured"):
        server.sign_encounter_note(note_id=note.note_id, content_hash=attestation.content_hash(note.note_text),
                                   attestation_token=token)


# ---------- 1.2 every way an attestation can be wrong ----------


def test_expired_wrong_action_wrong_record_wrong_content_are_each_refused():
    note, other = _reviewed_note(), _reviewed_note("A different note.")
    h = attestation.content_hash(note.note_text)
    cases = {
        "expired": _sign_token(note.note_id, note.note_text, now=time.time() - attestation.TTL_SECONDS - 5),
        "is for 'act_as'": _sign_token(note.note_id, note.note_text, action=attestation.ACT_AS),
        "different record": _sign_token(other.note_id, note.note_text),
        "different content": _sign_token(note.note_id, "tampered text"),
    }
    for reason, token in cases.items():
        with pytest.raises(ToolError, match=reason):
            server.sign_encounter_note(note_id=note.note_id, content_hash=h, attestation_token=token)
    assert not any(e["action"] == "sign_note" for e in _entries())


def test_a_used_attestation_cannot_be_replayed():
    note = server.generate_encounter_note(ehr_system="epic", patient_id="epic-pt-55219", transcript="t",
                                          note_text="Fatigue.", confidence=0.9)
    token = attestation.issue(_key(), "dr.rivera", "Dr. A. Rivera", attestation.ACT_AS, note.note_id,
                              attestation.ACT_AS_CONTENT)
    server.approve_encounter_note(note_id=note.note_id, actor_attestation=token)
    with pytest.raises(ToolError, match="already been used"):
        server.reject_encounter_note(note_id=note.note_id, feedback="x", actor_attestation=token)


def test_attestation_for_a_deactivated_clinician_is_refused():
    note = _reviewed_note()
    token = _sign_token(note.note_id, note.note_text)
    clinician_registry.set_active("dr.rivera", False)
    with pytest.raises(ToolError, match="not an active account"):
        server.sign_encounter_note(note_id=note.note_id, content_hash=attestation.content_hash(note.note_text),
                                   attestation_token=token)


# ---------- 1.3 only a reviewed, unchanged note can be signed ----------


def test_signing_an_unreviewed_note_is_refused():
    note = server.generate_encounter_note(ehr_system="epic", patient_id="epic-pt-55219", transcript="t",
                                          note_text="Draft.", confidence=0.9)
    with pytest.raises(ToolError, match="must be reviewed"):
        server.sign_encounter_note(note_id=note.note_id, content_hash=attestation.content_hash("Draft."),
                                   attestation_token=_sign_token(note.note_id, "Draft."))


def test_signing_a_rejected_note_is_refused():
    note = server.generate_encounter_note(ehr_system="epic", patient_id="epic-pt-55219", transcript="t",
                                          note_text="Draft.", confidence=0.9)
    server.reject_encounter_note(note_id=note.note_id, feedback="Wrong patient.")
    with pytest.raises(ToolError, match="must be reviewed"):
        server.sign_encounter_note(note_id=note.note_id, content_hash=attestation.content_hash("Draft."),
                                   attestation_token=_sign_token(note.note_id, "Draft."))


def test_signing_content_that_differs_from_the_reviewed_text_is_refused():
    note = _reviewed_note()
    with pytest.raises(ToolError, match="changed since it was reviewed"):
        server.sign_encounter_note(note_id=note.note_id, content_hash=attestation.content_hash("other"),
                                   attestation_token=_sign_token(note.note_id, "other"))


def test_unknown_note_is_not_found():
    with pytest.raises(ResourceNotFoundError):
        server.sign_encounter_note(note_id="nope", content_hash="0" * 64, attestation_token="a.b")


# ---------- 1.4 / 1.5 the signature, the lock, and idempotency ----------


def test_signed_note_records_who_when_and_what_and_is_locked():
    note = _reviewed_note()
    h = attestation.content_hash(note.note_text)
    signed = server.sign_encounter_note(note_id=note.note_id, content_hash=h,
                                        attestation_token=_sign_token(note.note_id, note.note_text))
    assert (signed.status, signed.signed_by_clinician_id, signed.content_hash) == ("signed", "dr.rivera", h)
    entry = _entries()[-1]
    assert entry["action"] == "sign_note" and entry["actor"]["type"] == "clinician"
    assert entry["actor"]["clinician_id"] == "dr.rivera" and entry["timestamp"] == signed.signed_at
    with pytest.raises(ToolError):
        server.edit_encounter_note(note_id=note.note_id, edited_note_text="changed after signing")
    assert item_status.derive_statuses(_entries())[("note", note.note_id)] == "signed"


def test_signing_twice_with_the_same_request_writes_once():
    note = _reviewed_note()
    h = attestation.content_hash(note.note_text)
    token = _sign_token(note.note_id, note.note_text)
    first = server.sign_encounter_note(note_id=note.note_id, content_hash=h, attestation_token=token)
    second = server.sign_encounter_note(note_id=note.note_id, content_hash=h, attestation_token=token)
    assert first == second
    assert [e["action"] for e in _entries()].count("sign_note") == 1


# ---------- 1.6 one note per signature ----------


def test_the_sign_tool_takes_exactly_one_note():
    schema = SNAPSHOT["tools"]["sign_encounter_note"]["input"]
    assert schema["properties"]["note_id"]["type"] == "string"
    assert set(schema["required"]) == {"note_id", "content_hash", "attestation_token"}
    assert not any(p.get("type") == "array" for p in schema["properties"].values())


# ---------- review identity ----------


def test_review_with_a_clinician_attestation_records_the_clinician():
    note = server.generate_encounter_note(ehr_system="epic", patient_id="epic-pt-55219", transcript="t",
                                          note_text="Fatigue.", confidence=0.9)
    token = attestation.issue(_key(), "dr.chen", "Dr. B. Chen", attestation.ACT_AS, note.note_id,
                              attestation.ACT_AS_CONTENT)
    server.approve_encounter_note(note_id=note.note_id, actor_attestation=token)
    entry = _entries()[-1]
    assert entry["stage"] == "reviewed" and entry["actor"]["clinician_id"] == "dr.chen"


def test_review_without_attestation_is_unverified_never_a_clinician():
    note = _reviewed_note()
    assert _entries()[-1]["actor"] == {"type": "unverified"}
    assert note.note_id


# ---------- 1.8 legacy notes ----------


def test_notes_approved_before_phase_1_read_as_legacy_approved():
    legacy = [{"action": "generate_note", "note_id": "n1"}, {"action": "approve_note", "note_id": "n1"}]
    current = [{"action": "generate_note", "note_id": "n2"}, {"action": "approve_note", "note_id": "n2", "stage": "reviewed"}]
    statuses = item_status.derive_statuses(legacy + current)
    assert statuses[("note", "n1")] == "approved"  # shown as "approved (legacy, unsigned)"
    assert statuses[("note", "n2")] == "clinician_reviewed"


# ---------- registry, sessions, protected files ----------


def test_registry_never_stores_secrets_and_checks_them():
    raw = Path(clinician_registry.registry_path()).read_text(encoding="utf-8")
    assert PASSWORD not in raw and PIN not in raw
    assert clinician_registry.verify_password("dr.rivera", PASSWORD).display_name == "Dr. A. Rivera"
    assert clinician_registry.verify_password("dr.rivera", "wrong password") is None
    assert clinician_registry.verify_password("nobody", PASSWORD) is None
    assert clinician_registry.verify_pin("dr.rivera", PIN) and not clinician_registry.verify_pin("dr.rivera", "0000")
    with pytest.raises(clinician_registry.RegistryError):
        clinician_registry.add_clinician("dr.rivera", "Dup", PASSWORD, PIN)
    with pytest.raises(clinician_registry.RegistryError):
        clinician_registry.add_clinician("dr.new", "New", "short", PIN)


def test_sessions_expire_and_signing_window_lasts_15_minutes():
    now = [1000.0]
    state = auth_sessions.AuthState(clock=lambda: now[0])
    s = state.create_session("dr.rivera", "Dr. A. Rivera")
    state.open_signing_window(s)
    now[0] += 14 * 60
    assert state.signing_window_open(s)
    now[0] += 2 * 60
    assert not state.signing_window_open(s)
    now[0] += auth_sessions.IDLE_SECONDS + 1
    assert state.get(s.token) is None


def test_protected_files_are_never_readable_as_transcripts(tmp_path):
    key_dir = attestation.key_dir()
    _key()
    for path in [key_dir / attestation.PRIVATE_KEY_NAME, clinician_registry.registry_path(),
                 tmp_path / ".env", app_core.AUDIT_LOG_PATH, tmp_path / "anything.pem"]:
        assert protected_paths.is_protected(path), path
    assert not protected_paths.is_protected(tmp_path / "visit_transcript.txt")


# ---------- 1.7 the web app: sign-in, CSRF, PIN lockout, signing ----------


async def _in_process(name, args):
    """Stand-in for the MCP session: call the server tool directly, as the bridge would remotely."""
    fn = getattr(server, name)
    try:
        if name == "record_auth_event":
            result = fn(event=AuthEvent(**args["event"]), attestation_token=args["attestation_token"])
        else:
            result = fn(**args)
    except (ToolError, ResourceNotFoundError) as exc:
        raise ToolCallError(400, str(exc)) from exc
    return result.model_dump(mode="json")


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(ui_bridge, "invoke_tool", _in_process)
    monkeypatch.setattr(ui_bridge.auth, "_call_tool", _in_process)
    monkeypatch.setattr(ui_bridge.auth, "state", auth_sessions.AuthState())
    return TestClient(ui_bridge.app)


def _login(client, clinician="dr.rivera"):
    r = client.post("/api/auth/login", json={"clinician_id": clinician, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def test_not_signed_in_is_401_everywhere_except_sign_in(web):
    for path in ["/api/tools/run_compliance_check", "/api/draft-note", "/api/transcribe", "/api/auth/reauth",
                 "/api/notes/n1/sign"]:
        assert web.post(path, json={}).status_code == 401, path
    assert web.get("/api/auth/me").status_code == 401


def test_missing_or_wrong_csrf_is_403(web):
    headers = _login(web)
    assert web.post("/api/tools/run_compliance_check", json={}).status_code == 403
    assert web.post("/api/tools/run_compliance_check", json={}, headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert web.post("/api/tools/run_compliance_check", json={}, headers=headers).status_code == 200


def test_wrong_password_is_refused_and_audited(web):
    r = web.post("/api/auth/login", json={"clinician_id": "dr.rivera", "password": "wrong password"})
    assert r.status_code == 401
    entry = _entries()[-1]
    assert (entry["action"], entry["kind"], entry["security_incident"]) == ("auth_event", "login_failure", True)


def test_five_failed_sign_ins_lock_the_account(web):
    for _ in range(5):
        web.post("/api/auth/login", json={"clinician_id": "dr.rivera", "password": "wrong password"})
    r = web.post("/api/auth/login", json={"clinician_id": "dr.rivera", "password": PASSWORD})
    assert r.status_code == 429


def test_five_wrong_pins_lock_re_authentication_with_an_audit_entry(web):
    headers = _login(web)
    codes = [web.post("/api/auth/reauth", json={"pin": "0000"}, headers=headers).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 401, 423]
    assert web.post("/api/auth/reauth", json={"pin": PIN}, headers=headers).status_code == 423
    kinds = [e["kind"] for e in _entries() if e["action"] == "auth_event"]
    assert kinds.count("reauth_failure") == 4 and kinds.count("reauth_locked") == 1


def test_full_flow_review_then_pin_then_sign_records_the_clinician(web):
    headers = _login(web, "dr.chen")
    note = web.post("/api/tools/generate_encounter_note", headers=headers, json={
        "ehr_system": "epic", "patient_id": "epic-pt-55219", "transcript": "Fatigue for six weeks.",
        "note_text": "Six weeks of fatigue.", "confidence": 0.9, "source": "ai"}).json()
    # A client-supplied attestation is discarded; the bridge attests the session's clinician.
    reviewed = web.post("/api/tools/approve_encounter_note", headers=headers,
                        json={"note_id": note["note_id"], "actor_attestation": "smuggled.value"})
    assert reviewed.status_code == 200, reviewed.text
    assert _entries()[-1]["actor"]["clinician_id"] == "dr.chen"
    h = attestation.content_hash("Six weeks of fatigue.")
    needs_pin = web.post(f"/api/notes/{note['note_id']}/sign", json={"content_hash": h}, headers=headers)
    assert needs_pin.status_code == 428 and needs_pin.json()["needs_pin"]
    assert web.post("/api/auth/reauth", json={"pin": PIN}, headers=headers).status_code == 200
    signed = web.post(f"/api/notes/{note['note_id']}/sign", json={"content_hash": h}, headers=headers)
    assert signed.status_code == 200, signed.text
    assert signed.json()["signed_by_clinician_id"] == "dr.chen"
    sign_entry = next(e for e in _entries() if e["action"] == "sign_note")
    assert sign_entry["actor"]["clinician_id"] == "dr.chen"


def test_the_browser_cannot_reach_the_sign_tool_through_the_passthrough(web):
    headers = _login(web)
    r = web.post("/api/tools/sign_encounter_note", headers=headers,
                 json={"note_id": "n", "content_hash": "0" * 64, "attestation_token": "x.y"})
    assert r.status_code == 403


def test_sign_in_that_cannot_be_audited_is_refused(web, monkeypatch):
    async def failing(name, args):
        raise ToolCallError(503, "audit unavailable")
    monkeypatch.setattr(ui_bridge.auth, "_call_tool", failing)
    r = web.post("/api/auth/login", json={"clinician_id": "dr.rivera", "password": PASSWORD})
    assert r.status_code == 503
    assert web.get("/api/auth/me").status_code == 401


def test_anyone_else_re_signing_a_signed_note_is_refused_not_handed_the_signature():
    note = _reviewed_note()
    h = attestation.content_hash(note.note_text)
    server.sign_encounter_note(note_id=note.note_id, content_hash=h,
                               attestation_token=_sign_token(note.note_id, note.note_text))
    for token in ["forged.token", _sign_token(note.note_id, note.note_text, clinician="dr.chen")]:
        with pytest.raises(ToolError, match="already signed"):
            server.sign_encounter_note(note_id=note.note_id, content_hash=h, attestation_token=token)
    assert [e["action"] for e in _entries()].count("sign_note") == 1
