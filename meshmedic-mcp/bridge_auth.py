"""Clinician sign-in, PIN re-entry and note signing for the web app (Phase 1, STORY-014).

Routes (all JSON):
    POST /api/auth/login    {clinician_id, password}  -> session cookie + CSRF token
    POST /api/auth/logout
    GET  /api/auth/me       -> who is signed in, CSRF token, signing window
    POST /api/auth/reauth   {pin}                     -> opens a 15-minute signing window
    POST /api/notes/{note_id}/sign {content_hash}     -> signs one reviewed note

Every other /api/ route needs a valid session (401 otherwise) and, for
anything but GET, the session's CSRF token in the X-CSRF-Token header (403).

The web app holds the attestation private key; it is the only thing that can
produce the attestations the MCP server requires for clinician-only actions.
Sign-in events are written to the audit trail through record_auth_event.
If a successful sign-in cannot be audited, the sign-in is refused.
"""

import secrets
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import attestation
import clinician_registry
from auth_sessions import AuthState, Session
from logging_utils import TOOL_ERROR, log_event, new_correlation_id

COOKIE = "mm_session"
OPEN_PATHS = {"/api/auth/login", "/api/auth/me"}
# Tools that record the signed-in clinician's identity (actor_attestation),
# and the argument the attestation is bound to.
ACTOR_AWARE_TOOLS = {
    "approve_encounter_note": "note_id",
    "edit_encounter_note": "note_id",
    "reject_encounter_note": "note_id",
    "start_encounter": "idempotency_key",
    "get_encounter_bundle": "encounter_id",
}

ToolCaller = Callable[[str, dict], Awaitable[dict]]


class ToolCallError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


class BridgeAuth:
    def __init__(self, call_tool: ToolCaller, logger, key_provider=attestation.load_or_create_private_key) -> None:
        self.state = AuthState()
        self._call_tool = call_tool
        self._logger = logger
        self._key_provider = key_provider

    # ---- helpers ----
    def session_for(self, request: Request) -> Session | None:
        return self.state.get(request.cookies.get(COOKIE))

    def issue(self, clinician_id: str, display_name: str, action: str, record_id: str, content_sha256: str) -> str:
        return attestation.issue(self._key_provider(), clinician_id, display_name, action, record_id, content_sha256)

    def attest_review(self, session: Session, tool_name: str, arguments: dict) -> dict:
        """Strip any client-supplied attestation; add one for the signed-in clinician where it applies."""
        arguments = {k: v for k, v in arguments.items() if k != "actor_attestation"}
        record_field = ACTOR_AWARE_TOOLS.get(tool_name)
        if record_field and isinstance(arguments.get(record_field), str):
            arguments["actor_attestation"] = self.issue(
                session.clinician_id, session.display_name, attestation.ACT_AS,
                arguments[record_field], attestation.ACT_AS_CONTENT,
            )
        return arguments

    async def audit(self, kind: str, clinician_id: str | None, detail: str | None = None) -> bool:
        event = {"event_id": secrets.token_hex(12), "kind": kind, "clinician_id": clinician_id, "detail": detail}
        token = self.issue(attestation.WEB_APP_IDENTITY, "MeshMedic web app", attestation.AUTH_EVENT, event["event_id"],
                           attestation.event_hash(event))
        try:
            await self._call_tool("record_auth_event", {"event": event, "attestation_token": token})
            return True
        except ToolCallError:
            log_event(self._logger, "error", TOOL_ERROR, new_correlation_id(), tool="record_auth_event",
                      kind=kind, error_class="AuditLogWriteFailure")
            return False

    # ---- guard for every /api/ request ----
    def guard(self, request: Request) -> JSONResponse | None:
        path = request.url.path
        if not path.startswith("/api/") or path in OPEN_PATHS:
            return None
        session = self.session_for(request)
        if session is None:
            return _error(401, "Sign in to MeshMedic first.", signed_in=False)
        if request.method != "GET" and request.headers.get("x-csrf-token") != session.csrf_token:
            return _error(403, "Missing or invalid CSRF token. Reload the page and try again.")
        request.state.session = session
        return None

    # ---- routes ----
    async def login(self, request: Request) -> JSONResponse:
        body = await _json(request)
        clinician_id = str(body.get("clinician_id") or "").strip().lower()
        password = str(body.get("password") or "")
        if not clinician_id or not password:
            return _error(400, "Enter your clinician ID and password.")
        if self.state.login_locked(clinician_id):
            await self.audit("login_failure", clinician_id, "account locked")
            return _error(429, "Too many failed sign-ins. Try again in 15 minutes.")
        clinician = clinician_registry.verify_password(clinician_id, password)
        if clinician is None:
            locked = self.state.record_login_failure(clinician_id)
            await self.audit("login_failure", clinician_id, "locked after repeated failures" if locked else None)
            return _error(401, "Clinician ID or password is incorrect.")
        session = self.state.create_session(clinician.clinician_id, clinician.display_name)
        if not await self.audit("login_success", clinician.clinician_id):
            self.state.end(session.token)
            return _error(503, "Sign-in could not be recorded in the audit trail, so it was refused. Try again.")
        response = JSONResponse(_me(session, self.state))
        response.set_cookie(COOKIE, session.token, httponly=True, samesite="strict", path="/",
                            secure=request.url.scheme == "https")
        return response

    async def logout(self, request: Request) -> JSONResponse:
        session = self.state.end(request.cookies.get(COOKIE))
        if session:
            await self.audit("logout", session.clinician_id)
        response = JSONResponse({"signed_in": False})
        response.delete_cookie(COOKIE, path="/")
        return response

    async def me(self, request: Request) -> JSONResponse:
        session = self.session_for(request)
        if session is None:
            return _error(401, "Not signed in.", signed_in=False)
        return JSONResponse(_me(session, self.state))

    async def reauth(self, request: Request) -> JSONResponse:
        session: Session = request.state.session
        pin = str((await _json(request)).get("pin") or "")
        if self.state.pin_locked(session.clinician_id):
            return _error(423, "Signing PIN entry is locked after repeated failures. Try again in 15 minutes.")
        if not clinician_registry.verify_pin(session.clinician_id, pin):
            locked = self.state.record_pin_failure(session.clinician_id)
            await self.audit("reauth_locked" if locked else "reauth_failure", session.clinician_id)
            if locked:
                return _error(423, "Too many wrong PINs. Signing PIN entry is locked for 15 minutes.")
            return _error(401, "That signing PIN is incorrect.")
        self.state.open_signing_window(session)
        await self.audit("reauth_success", session.clinician_id)
        return JSONResponse(_me(session, self.state))

    async def sign_note(self, request: Request) -> JSONResponse:
        session: Session = request.state.session
        note_id = request.path_params["note_id"]
        content_sha256 = str((await _json(request)).get("content_hash") or "")
        if not self.state.signing_window_open(session):
            return _error(428, "Re-enter your signing PIN to sign.", needs_pin=True)
        token = self.issue(session.clinician_id, session.display_name, attestation.SIGN_NOTE, note_id, content_sha256)
        try:
            result = await self._call_tool(
                "sign_encounter_note", {"note_id": note_id, "content_hash": content_sha256, "attestation_token": token}
            )
        except ToolCallError as exc:
            return _error(exc.status, str(exc))
        return JSONResponse(result)

    def routes(self) -> list[Route]:
        return [
            Route("/api/auth/login", self.login, methods=["POST"]),
            Route("/api/auth/logout", self.logout, methods=["POST"]),
            Route("/api/auth/me", self.me, methods=["GET"]),
            Route("/api/auth/reauth", self.reauth, methods=["POST"]),
            Route("/api/notes/{note_id}/sign", self.sign_note, methods=["POST"]),
        ]


def _me(session: Session, state: AuthState) -> dict:
    return {
        "signed_in": True,
        "clinician_id": session.clinician_id,
        "display_name": session.display_name,
        "csrf_token": session.csrf_token,
        "signing_window_open": state.signing_window_open(session),
        "signing_until": session.signing_until or None,
    }


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
