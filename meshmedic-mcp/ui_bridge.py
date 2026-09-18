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
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

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
DRAFT_MAX_TOKENS = 1500
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
        _anthropic = anthropic.Anthropic()
    if os.environ.get("OPENAI_API_KEY"):
        _openai = openai.OpenAI()
    try:
        yield
    finally:
        await _stack.aclose()


def _result_payload(content: list[Any]) -> dict[str, Any]:
    """Fallback for a tool result with no structured_content: join any text blocks."""
    text_parts = [block.text for block in content if getattr(block, "text", None)]
    return {"text": "\n".join(text_parts) if text_parts else ""}


async def call_tool(request: Request) -> JSONResponse:
    """Proxy one POST /api/tools/<name> call straight to the real MCP tool.

    The request body (JSON object, `{}` if empty) becomes the tool's
    arguments unchanged -- this bridge does not validate or reshape them,
    the tool's own schema does. A tool-level failure (ToolError,
    ResourceNotFoundError) comes back from the SDK as `is_error=True` rather
    than an exception, so it is reported here as a 400 with the tool's own
    message, not swallowed and not raised as a bridge-side 500.
    """
    tool_name = request.path_params["tool_name"]
    try:
        arguments = await request.json()
    except Exception:
        arguments = {}

    if _session is None:
        return JSONResponse({"error": "MCP session is not connected yet"}, status_code=503)

    try:
        result = await _session.call_tool(tool_name, arguments)
    except Exception as exc:
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=502)

    payload = result.structured_content
    if payload is None:
        payload = _result_payload(result.content)

    if result.is_error:
        message = payload.get("text") if isinstance(payload, dict) else str(payload)
        return JSONResponse({"error": message or "Tool call failed"}, status_code=400)

    return JSONResponse(payload)


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
        response = _anthropic.messages.create(
            model=DRAFT_MODEL,
            max_tokens=DRAFT_MAX_TOKENS,
            system=DRAFT_SYSTEM_PROMPT,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": transcript}],
        )
    except anthropic.AuthenticationError:
        return JSONResponse({"error": "Anthropic API key was rejected (AuthenticationError)."}, status_code=502)
    except anthropic.RateLimitError:
        return JSONResponse({"error": "Rate limited by the Anthropic API. Try again shortly."}, status_code=502)
    except anthropic.APIStatusError as exc:
        return JSONResponse({"error": f"Anthropic API error ({exc.status_code}): {exc.message}"}, status_code=502)
    except anthropic.APIConnectionError:
        return JSONResponse({"error": "Could not reach the Anthropic API (network error)."}, status_code=502)

    note_text = next((b.text for b in response.content if b.type == "text"), "").strip()
    return JSONResponse(
        {
            "note_text": note_text,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }
    )


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
        result = _openai.audio.transcriptions.create(
            model=TRANSCRIBE_MODEL,
            file=(upload.filename or "audio.webm", audio_bytes, upload.content_type or "audio/webm"),
            prompt=TRANSCRIBE_VOCAB_HINT,
        )
    except openai.AuthenticationError:
        return JSONResponse({"error": "OpenAI API key was rejected (AuthenticationError)."}, status_code=502)
    except openai.RateLimitError as exc:
        # OpenAI returns HTTP 429 for both true rate limiting and an
        # exhausted/unfunded account ("insufficient_quota") -- the SDK maps
        # both to RateLimitError, so the body's error code is the only way
        # to tell "wait and retry" from "add billing" apart. Surface it.
        body = exc.body if isinstance(exc.body, dict) else {}
        if body.get("code") == "credit_balance_exhausted" or body.get("type") == "insufficient_quota":
            return JSONResponse(
                {"error": "OpenAI account has no credits. Add billing at platform.openai.com, then try again."},
                status_code=502,
            )
        return JSONResponse({"error": "Rate limited by the OpenAI API. Try again shortly."}, status_code=502)
    except openai.APIStatusError as exc:
        return JSONResponse({"error": f"OpenAI API error ({exc.status_code}): {exc.message}"}, status_code=502)
    except openai.APIConnectionError:
        return JSONResponse({"error": "Could not reach the OpenAI API (network error)."}, status_code=502)

    return JSONResponse({"transcript": result.text})


routes = [
    Route("/api/tools/{tool_name}", call_tool, methods=["POST"]),
    Route("/api/draft-note", draft_note, methods=["POST"]),
    Route("/api/transcribe", transcribe_audio, methods=["POST"]),
    Mount("/", app=StaticFiles(directory=str(STATIC_DIR), html=True), name="static"),
]

middleware = [
    Middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:8420", "http://localhost:8420"], allow_methods=["*"], allow_headers=["*"]),
]

app = Starlette(routes=routes, middleware=middleware, lifespan=_lifespan)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8420)
