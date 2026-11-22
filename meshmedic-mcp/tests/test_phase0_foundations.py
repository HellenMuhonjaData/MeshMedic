"""Phase 0 acceptance tests: foundations with no behaviour change.

0.1 lives in test_tool_contract.py (tool names and schemas unchanged).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import app_core
import audit_actor
import audit_store
import auth_sessions
import bridge_auth
import encounters
import item_status
import passthrough_policy
import runtime_config
import server
import ui_bridge

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = json.loads((Path(__file__).parent / "tool_contract_snapshot.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(app_core, "AUDIT_LOG_PATH", tmp_path / "audit_log.jsonl")


def _entries():
    path = app_core.AUDIT_LOG_PATH
    return audit_store.read_entries(path)


# ---------- 0.2 pass-through allowlist ----------


def test_every_existing_tool_is_still_reachable_from_the_browser():
    assert set(SNAPSHOT["tools"]) - passthrough_policy.ATTESTED_TOOLS <= passthrough_policy.PASSTHROUGH_TOOLS


def test_no_attested_tool_is_on_the_passthrough_list():
    assert not (passthrough_policy.PASSTHROUGH_TOOLS & passthrough_policy.ATTESTED_TOOLS)


def _signed_in_client(monkeypatch):
    """A web-app client with a signed-in session (Phase 1 requires one for every /api/ call)."""
    state = auth_sessions.AuthState()
    monkeypatch.setattr(ui_bridge.auth, "state", state)
    session = state.create_session("dr.test", "Dr. Test")
    client = TestClient(ui_bridge.app)  # no lifespan: the MCP server is never started
    client.cookies.set(bridge_auth.COOKIE, session.token)
    client.headers["X-CSRF-Token"] = session.csrf_token
    return client


@pytest.mark.parametrize("tool", ["sign_encounter_note", "authorize_orders", "some_future_tool"])
def test_passthrough_refuses_unlisted_and_attested_tools_without_touching_the_audit_trail(tool, monkeypatch):
    client = _signed_in_client(monkeypatch)
    response = client.post(f"/api/tools/{tool}", json={"note_id": "x"})
    assert response.status_code == 403
    assert _entries() == []


def test_passthrough_still_forwards_listed_tools(monkeypatch):
    client = _signed_in_client(monkeypatch)  # no MCP server running, so a listed tool gets "not connected"
    assert client.post("/api/tools/run_compliance_check", json={}).status_code == 503


# ---------- 0.3 actor on every new entry ----------


def _note(source):
    return server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-55219",
        transcript="Fatigue for six weeks.",
        note_text="Patient reports six weeks of fatigue.",
        confidence=0.9,
        source=source,
    )


def test_ai_draft_is_recorded_with_an_ai_actor_and_decisions_as_unverified():
    note = _note("ai")
    server.approve_encounter_note(note_id=note.note_id)
    generated, approved = _entries()
    assert generated["actor"] == {"type": "ai"}
    assert approved["actor"] == {"type": "unverified"}


def test_clinician_typed_note_is_unverified_until_sign_in_exists():
    _note("clinician")
    assert _entries()[0]["actor"] == {"type": "unverified"}


def test_suggestions_record_an_ai_actor():
    note = _note("ai")
    server.record_ai_suggestions(note_id=note.note_id, care_gaps=[], test_orders=[])
    assert _entries()[-1]["actor"] == {"type": "ai"}


def test_entries_without_an_actor_read_as_legacy():
    assert audit_actor.entry_actor({"action": "approve_note"}) == {"type": "legacy"}
    assert audit_actor.entry_actor({"actor": {"type": "ai"}}) == {"type": "ai"}
    with pytest.raises(ValueError):
        audit_actor.clinician("")


# ---------- 0.4 synthetic vs real data ----------


def _ready_file(tmp_path, items=runtime_config.REQUIRED_READINESS_ITEMS, **override):
    data = {"items": {i: {"verified_by": "Privacy Officer", "verified_on": "2026-11-20"} for i in items}}
    data["items"].update(override)
    path = tmp_path / "ready.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_synthetic_is_the_default_with_seven_day_audio_retention(tmp_path):
    cfg = runtime_config.load_config({}, tmp_path / "none.json")
    assert (cfg.data_mode, cfg.audio_retention_days) == ("synthetic", 7)
    assert runtime_config.load_config({"MESHMEDIC_AUDIO_RETENTION_DAYS": "3"}).audio_retention_days == 3


@pytest.mark.parametrize(
    "env",
    [
        {"MESHMEDIC_DATA_MODE": "real"},  # no retention set
        {"MESHMEDIC_DATA_MODE": "real", "MESHMEDIC_AUDIO_RETENTION_DAYS": "30"},  # no policy reference
        {"MESHMEDIC_DATA_MODE": "production"},  # unknown mode
        {"MESHMEDIC_AUDIO_RETENTION_DAYS": "-1"},
        {"MESHMEDIC_AUDIO_RETENTION_DAYS": "seven"},
    ],
)
def test_unapproved_configurations_are_refused(env, tmp_path):
    with pytest.raises(runtime_config.ConfigError):
        runtime_config.load_config(env, _ready_file(tmp_path))


REAL = {
    "MESHMEDIC_DATA_MODE": "real",
    "MESHMEDIC_AUDIO_RETENTION_DAYS": "30",
    "MESHMEDIC_RETENTION_POLICY_REF": "ORG-REC-RET-2026-04",
}


def test_real_mode_needs_the_readiness_file(tmp_path):
    with pytest.raises(runtime_config.ConfigError, match="does not exist"):
        runtime_config.load_config(REAL, tmp_path / "missing.json")


def test_real_mode_names_every_missing_sign_off(tmp_path):
    partial = _ready_file(tmp_path, items=runtime_config.REQUIRED_READINESS_ITEMS[:-2])
    with pytest.raises(runtime_config.ConfigError) as err:
        runtime_config.load_config(REAL, partial)
    for item in runtime_config.REQUIRED_READINESS_ITEMS[-2:]:
        assert item in str(err.value)


def test_real_mode_rejects_a_bad_sign_off_date(tmp_path):
    bad = _ready_file(tmp_path, openai_baa_executed={"verified_by": "x", "verified_on": "soon"})
    with pytest.raises(runtime_config.ConfigError, match="invalid verified_on"):
        runtime_config.load_config(REAL, bad)


def test_real_mode_starts_only_when_everything_is_signed_off(tmp_path):
    cfg = runtime_config.load_config(REAL, _ready_file(tmp_path))
    assert (cfg.data_mode, cfg.audio_retention_days, cfg.retention_policy_ref) == ("real", 30, "ORG-REC-RET-2026-04")


def test_server_process_refuses_to_start_in_real_mode_without_sign_off():
    env = {**os.environ, "MESHMEDIC_DATA_MODE": "real"}
    result = subprocess.run(
        [sys.executable, "server.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode != 0
    assert "MeshMedic will not start" in result.stderr


# ---------- status vocabulary and encounter IDs ----------


def test_statuses_follow_the_audit_trail():
    note = _note("ai")
    rec = server.record_ai_suggestions(
        note_id=note.note_id,
        test_orders=[server.TestOrderSuggestion(test_name="A1c", reason="Confirm diabetes")],
    )
    item = rec.items[0]
    statuses = item_status.derive_statuses(_entries())
    assert statuses[("note", note.note_id)] == "ai_suggested"
    assert statuses[("suggestion", item.item_id)] == "ai_suggested"
    order = server.order_test(note_id=note.note_id, test_name="A1c", reason="Confirm diabetes",
                              suggestion_item_id=item.item_id)
    server.approve_encounter_note(note_id=note.note_id)
    statuses = item_status.derive_statuses(_entries())
    assert statuses[("note", note.note_id)] == "clinician_reviewed"
    assert statuses[("suggestion", item.item_id)] == "approved"
    assert statuses[("test_order", order.order_id)] == "approved"


def test_encounter_ids_are_random_and_carry_no_patient_data():
    a, b = encounters.new_encounter_id(), encounters.new_encounter_id()
    assert a != b and encounters.is_encounter_id(a)
    assert not encounters.is_encounter_id("enc_E55219")
    assert not encounters.is_encounter_id(None)


# ---------- 0.5 module size ----------

SPLIT_MODULES = [
    "server", "app_core", "models", "audit_queries", "audit_actor", "passthrough_policy", "runtime_config",
    "encounters", "item_status", *sorted(p.stem for p in ROOT.glob("tools_*.py")),
]


@pytest.mark.parametrize("module", SPLIT_MODULES)
def test_no_new_module_exceeds_500_lines(module):
    lines = (ROOT / f"{module}.py").read_text(encoding="utf-8").count("\n")
    assert lines <= 500, f"{module}.py has {lines} lines"
