"""HTTP bridge exposing meshmedic-mcp's tools to the clinician web UI (REQ-017).

meshmedic-mcp speaks MCP over stdio to exactly one connected client for its
whole process lifetime (see docs/TRANSPORT_DECISION.md) -- a browser cannot
open a stdio pipe, so this process *is* that one client: it spawns the real
server as a subprocess, keeps one long-lived MCP session open for as long as
this bridge runs, and re-exposes each tool call as a plain HTTP POST for the
static frontend in ui/static/ to call.

No business logic lives here. Every request is a pass-through to the real
tool, so the audit trail, the Epic sandbox call, and the compliance check the
UI shows are the real ones this session produced, not a mock.

Run with: uv run python ui_bridge.py
"""

import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import anthropic
import openai
import uvicorn
from dotenv import load_dotenv
from mcp import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import ai_services
import attestation
import job_store
import runtime_config
from bridge_auth import BridgeAuth, ToolCallError
from encounter_routes import EncounterRoutes, purge_expired_audio
from encounter_worker import EncounterWorker
from passthrough_policy import is_passthrough_allowed

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "ui" / "static"

load_dotenv(ROOT / ".env")

# REQ-017 AI-drafting: same model this project's other agent-facing surfaces
# default to (see the claude-api skill's "always claude-opus-5 unless the
# user says otherwise" rule). "low" effort because drafting one note from one
# transcript is a bounded extraction/summarization task, not deep reasoning --
# keeps latency and per-call cost down without a quality tradeoff worth caring
# about here (see PROGRESS.md for the per-call cost estimate this was chosen
# against).
DRAFT_MODEL = "claude-opus-5"
DRAFT_MAX_TOKENS = 4000  # note + codes + gaps + orders + referral as JSON
DRAFT_SYSTEM_PROMPT = (ROOT.parent / "prompts" / "generate-clinical-note" / "system_prompt.md").read_text()

# REQ-015 (must, constraint): "ambient audio recordings and text transcripts"
# both need to be valid input for note generation. Claude's Messages API has
# no audio input type as of this writing -- text, images, and PDFs only --
# so transcription is a second provider by necessity, not by choice.
TRANSCRIBE_MODEL = "whisper-1"

# Whisper's `prompt` is a vocabulary hint, not an instruction -- biasing it
# with real clinical/medication terms measurably cuts mis-transcriptions
# like "lisinopril" -> "LIS in April" (an actual failure seen while testing
# this). Short on purpose: Whisper only reads roughly the last 224 tokens of
# the prompt as context.
TRANSCRIBE_VOCAB_HINT = (
    "Clinical encounter. Medications: lisinopril, metformin, atorvastatin, "
    "amlodipine, losartan, albuterol, levothyroxine, omeprazole. Terms: "
    "hypertension, hyperlipidemia, HbA1c, blood pressure, dyspnea, "
    "tachycardia, edema, systolic, diastolic, milligrams."
)

_session: ClientSession | None = None
_stack: AsyncExitStack | None = None
_anthropic: anthropic.Anthropic | None = None
_openai: openai.OpenAI | None = None


@asynccontextmanager
async def _lifespan(app: Starlette):
    global _session, _stack, _anthropic, _openai
    # The web app holds the attestation signing key; the MCP server it starts
    # reads only the public half. Create the pair before starting the server.
    attestation.load_or_create_private_key()
    _stack = AsyncExitStack()
    # --no-sync: this venv already has everything server.py needs, and a
    # `uv run` sync at spawn time can collide with another already-running
    # server.py instance (Inspector's, another bridge, ...) holding this
    # package's dist-info open -- that collision aborts the spawn entirely.
    params = StdioServerParameters(command="uv", args=["run", "--no-sync", "python", "server.py"], cwd=str(ROOT))
    read, write = await _stack.enter_async_context(stdio_client(params))
    session = await _stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    _session = session
    if os.environ.get("ANTHROPIC_API_KEY"):
        _anthropic = ai_services.anthropic_client()
    if os.environ.get("OPENAI_API_KEY"):
        _openai = ai_services.openai_client()
    try:
        stop = asyncio.Event()
        workers = [
            asyncio.create_task(EncounterWorker(
                jobs, invoke_tool, _transcribe_for_worker, _draft_for_worker,
                f"worker-{i}", _bridge_logger, ToolCallError,
            ).run_forever(stop))
            for i in range(WORKER_COUNT)
        ]
        retention = asyncio.create_task(_retention_loop(stop))
        yield
    finally:
        if "stop" in locals():
            stop.set()
            await asyncio.gather(*workers, retention, return_exceptions=True)
        await _stack.aclose()


def _result_payload(content: list[Any]) -> dict[str, Any]:
    """Fallback for a tool result with no structured_content: join any text blocks."""
    text_parts = [block.text for block in content if getattr(block, "text", None)]
    return {"text": "\n".join(text_parts) if text_parts else ""}


async def invoke_tool(tool_name: str, arguments: dict) -> dict:
    """Call one MCP tool; a tool-level failure becomes ToolCallError(400) with the tool's own message."""
    if _session is None:
        raise ToolCallError(503, "MCP session is not connected yet")
    try:
        result = await _session.call_tool(tool_name, arguments)
    except Exception as exc:
        raise ToolCallError(502, f"{type(exc).__name__}: {exc}") from exc
    payload = result.structured_content
    if payload is None:
        payload = _result_payload(result.content)
    if result.is_error:
        message = payload.get("text") if isinstance(payload, dict) else str(payload)
        raise ToolCallError(400, message or "Tool call failed")
    return payload


_bridge_logger = logging.getLogger("meshmedic_bridge")
auth = BridgeAuth(invoke_tool, _bridge_logger)

# Phase 2: background processing. Job progress lives in SQLite (no clinical
# content); the clinical record stays the audit trail.
JOBS_DB = Path(os.environ.get("MESHMEDIC_JOBS_DB") or ROOT / "jobs.sqlite3")
jobs = job_store.SqliteJobStore(JOBS_DB)
encounter_api = EncounterRoutes(auth, invoke_tool, jobs, _bridge_logger)
WORKER_COUNT = 2
RETENTION_SWEEP_SECONDS = 3600


async def _transcribe_for_worker(audio: bytes, filename: str, content_type: str) -> str:
    if _openai is None:
        raise ai_services.PermanentAIError("OPENAI_API_KEY is not set, so recordings cannot be transcribed.")
    return await ai_services.transcribe(_openai, TRANSCRIBE_MODEL, TRANSCRIBE_VOCAB_HINT, audio, filename, content_type)


async def _draft_for_worker(transcript: str) -> dict:
    if _anthropic is None:
        raise ai_services.PermanentAIError("ANTHROPIC_API_KEY is not set, so encounters cannot be drafted.")
    draft, _usage = await ai_services.draft_encounter(
        _anthropic, DRAFT_MODEL, DRAFT_MAX_TOKENS, DRAFT_SYSTEM_PROMPT, transcript
    )
    return ai_services.draft_to_dict(draft)


async def _retention_loop(stop: asyncio.Event) -> None:
    config = runtime_config.load_config(os.environ)
    policy = config.retention_policy_ref or f"synthetic-prototype-{config.audio_retention_days}-day"
    while not stop.is_set():
        try:
            await purge_expired_audio(invoke_tool, auth, config.audio_retention_days, policy, _bridge_logger)
        except ToolCallError:
            _bridge_logger.exception("audio retention sweep failed; it will run again")
        try:
            await asyncio.wait_for(stop.wait(), timeout=RETENTION_SWEEP_SECONDS)
        except asyncio.TimeoutError:
            pass  # time for the next sweep


async def call_tool(request: Request) -> JSONResponse:
    """Proxy one POST /api/tools/<name> call to the real MCP tool, for a signed-in clinician.

    The request body (JSON object, `{}` if empty) becomes the tool's
    arguments -- the tool's own schema validates them. Two things are
    changed on the way: tools not on the pass-through allowlist are refused
    (403) before reaching the server, and for note review tools the bridge
    attaches an attestation of the signed-in clinician's identity (any
    client-supplied one is discarded), so the audit trail records who acted.
    """
    tool_name = request.path_params["tool_name"]
    if not is_passthrough_allowed(tool_name):
        # Checked before anything else: a refused tool never reaches the MCP
        # server, so nothing is executed or written to the audit trail.
        return JSONResponse(
            {"error": f"{tool_name!r} cannot be called from the browser through this route."},
            status_code=403,
        )
    try:
        arguments = await request.json()
    except ValueError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    arguments = auth.attest_review(request.state.session, tool_name, arguments)
    try:
        return JSONResponse(await invoke_tool(tool_name, arguments))
    except ToolCallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


async def draft_note(request: Request) -> JSONResponse:
    """POST /api/draft-note: AI-draft note_text from a transcript (REQ-017).

    This is a convenience for the UI only -- it never touches the audit
    trail or any MCP tool. The clinician still reviews (and can edit) the
    draft before generate_encounter_note persists anything, same as if
    they'd typed it themselves.
    """
    if _anthropic is None:
        return JSONResponse(
            {"error": "ANTHROPIC_API_KEY is not set. Add it to meshmedic-mcp/.env and restart the bridge."},
            status_code=503,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    transcript = (body.get("transcript") or "").strip()
    if not transcript:
        return JSONResponse({"error": "transcript is required"}, status_code=400)

    try:
        draft, usage = await ai_services.draft_encounter(
            _anthropic, DRAFT_MODEL, DRAFT_MAX_TOKENS, DRAFT_SYSTEM_PROMPT, transcript
        )
    except ai_services.AIServiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({**ai_services.draft_to_dict(draft), "usage": usage})


async def transcribe_audio(request: Request) -> JSONResponse:
    """POST /api/transcribe: turn an uploaded/recorded audio clip into text (REQ-015).

    Accepts multipart/form-data with one field, `audio` -- either a recorded
    browser clip (webm/opus from MediaRecorder) or an uploaded file. Returns
    plain text; nothing is persisted here, same convention as /api/draft-note.
    """
    if _openai is None:
        return JSONResponse(
            {"error": "OPENAI_API_KEY is not set. Add it to meshmedic-mcp/.env and restart the bridge."},
            status_code=503,
        )
    form = await request.form()
    upload = form.get("audio")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "multipart field 'audio' (a file) is required"}, status_code=400)

    audio_bytes = await upload.read()
    if not audio_bytes:
        return JSONResponse({"error": "audio file is empty"}, status_code=400)

    try:
        text = await ai_services.transcribe(
            _openai, TRANSCRIBE_MODEL, TRANSCRIBE_VOCAB_HINT, audio_bytes,
            upload.filename or "audio.webm", upload.content_type or "audio/webm",
        )
    except ai_services.AIServiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"transcript": text})


routes = [
    *auth.routes(),
    *encounter_api.routes(),
    Route("/api/tools/{tool_name}", call_tool, methods=["POST"]),
    Route("/api/draft-note", draft_note, methods=["POST"]),
    Route("/api/transcribe", transcribe_audio, methods=["POST"]),
    Mount("/", app=StaticFiles(directory=str(STATIC_DIR), html=True), name="static"),
]

async def _no_stale_ui(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


async def _require_session(request: Request, call_next):
    """Every /api/ route except sign-in needs a session, and a CSRF token for writes."""
    refusal = auth.guard(request)
    if refusal is not None:
        return refusal
    return await call_next(request)


middleware = [
    # Pages and styles must always be revalidated: without this the browser
    # may reuse a cached copy of the UI after it changes (heuristic caching).
    Middleware(BaseHTTPMiddleware, dispatch=_no_stale_ui),
    Middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:8420", "http://localhost:8420"], allow_methods=["*"], allow_headers=["*"]),
    Middleware(BaseHTTPMiddleware, dispatch=_require_session),
]

app = Starlette(routes=routes, middleware=middleware, lifespan=_lifespan)


if __name__ == "__main__":
    # Refuse to start if this configuration is not approved (e.g. real-data
    # mode without a signed-off readiness file). See runtime_config.py.
    try:
        runtime_config.load_config(os.environ)
    except runtime_config.ConfigError as exc:
        raise SystemExit(f"MeshMedic will not start: {exc}") from exc
    uvicorn.run(app, host="127.0.0.1", port=8420)
