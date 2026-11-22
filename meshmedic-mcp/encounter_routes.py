"""Web-app routes for encounters, the review queue and audio retention (Phase 2, STORY-015).

    POST /api/encounters                     {ehr_system, patient_id, idempotency_key} -> start an encounter
    POST /api/encounters/{id}/audio          multipart "audio"                         -> store the recording (encrypted)
    POST /api/encounters/{id}/transcript     {text}                                    -> store a typed transcript
    POST /api/encounters/{id}/end                                                      -> queue background processing
    POST /api/encounters/{id}/retry                                                    -> retry one that needs attention
    GET  /api/encounters/{id}                                                          -> open for review (from the server)
    GET  /api/encounters/{id}/audio                                                    -> play back the recording
    GET  /api/queue                                                                    -> this clinician's encounters

Only the clinician who started an encounter can add to it, open it or hear
its audio; anyone else gets 403. Every route needs a signed-in session
(bridge_auth.BridgeAuth.guard).
"""

import json
import logging
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import attestation
import encounter_files
import encounters
import job_store
from bridge_auth import BridgeAuth, ToolCallError
from logging_utils import TOOL_ERROR, log_event, new_correlation_id

_PROCESSING = {"queued", "running", "retry_wait"}


def queue_status(job: job_store.Job | None, note_status: str | None) -> str:
    """One word for where an encounter is, combining background progress and clinical review."""
    if job is None:
        return "capturing"
    if job.status in _PROCESSING:
        return "processing"
    if job.status == "needs_attention":
        return "needs_attention"
    if note_status == "signed":
        return "signed"
    if note_status in ("clinician_reviewed", "approved_legacy_unsigned"):
        return "awaiting_signature"
    if note_status == "rejected":
        return "rejected"
    return "ready_for_review"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class EncounterRoutes:
    def __init__(self, auth: BridgeAuth, call_tool, store: job_store.JobStore, logger: logging.Logger) -> None:
        self.auth = auth
        self._call_tool = call_tool
        self.store = store
        self._logger = logger

    async def _tool(self, request: Request, name: str, args: dict) -> dict:
        return await self._call_tool(name, self.auth.attest_review(request.state.session, name, args))

    async def _mine(self, request: Request) -> list[dict]:
        listing = await self._call_tool("list_encounters", {"started_by": request.state.session.clinician_id})
        return listing["encounters"]

    async def _owned(self, request: Request) -> dict | None:
        enc = request.path_params["encounter_id"]
        if not encounters.is_encounter_id(enc):
            return None
        return next((e for e in await self._mine(request) if e["encounter_id"] == enc), None)

    async def start(self, request: Request) -> JSONResponse:
        body = await _json(request)
        args = {k: body.get(k) for k in ("ehr_system", "patient_id", "idempotency_key")}
        try:
            encounter = await self._tool(request, "start_encounter", args)
        except ToolCallError as exc:
            return _error(exc.status, str(exc))
        context = {"ehr_system": encounter["ehr_system"], "patient_id": encounter["patient_id"]}
        encounter_files.write(encounter["encounter_id"], "context", json.dumps(context).encode("utf-8"))
        return JSONResponse(encounter)

    async def upload_audio(self, request: Request) -> JSONResponse:
        enc = await self._owned(request)
        if enc is None:
            return _error(403, "That encounter is not yours or does not exist.")
        form = await request.form()
        upload = form.get("audio")
        if upload is None or not hasattr(upload, "read"):
            return _error(400, "multipart field 'audio' (a file) is required")
        data = await upload.read()
        if not data:
            return _error(400, "audio file is empty")
        try:
            encounter_files.write(enc["encounter_id"], "audio", data, meta={
                "filename": "audio" + (".webm" if "webm" in (upload.content_type or "") else ".wav"),
                "content_type": upload.content_type or "audio/webm", "bytes": len(data)})
        except encounter_files.EncounterFileError as exc:
            return _error(400, str(exc))
        return JSONResponse({"stored": True, "bytes": len(data)})

    async def save_transcript(self, request: Request) -> JSONResponse:
        enc = await self._owned(request)
        if enc is None:
            return _error(403, "That encounter is not yours or does not exist.")
        text = str((await _json(request)).get("text") or "").strip()
        if not text:
            return _error(400, "The transcript is empty.")
        encounter_files.write(enc["encounter_id"], "transcript", text.encode("utf-8"))
        return JSONResponse({"stored": True})

    async def end(self, request: Request) -> JSONResponse:
        enc = await self._owned(request)
        if enc is None:
            return _error(403, "That encounter is not yours or does not exist.")
        eid = enc["encounter_id"]
        if not (encounter_files.exists(eid, "audio") or encounter_files.exists(eid, "transcript")):
            return _error(400, "Record or type the consultation before ending it.")
        job = self.store.enqueue(eid)
        return JSONResponse({"encounter_id": eid, "status": queue_status(job, None)}, status_code=202)

    async def retry(self, request: Request) -> JSONResponse:
        enc = await self._owned(request)
        if enc is None:
            return _error(403, "That encounter is not yours or does not exist.")
        job = self.store.retry_now(enc["encounter_id"])
        return JSONResponse({"encounter_id": enc["encounter_id"], "status": queue_status(job, enc["note_status"])})

    async def queue(self, request: Request) -> JSONResponse:
        items = []
        for e in await self._mine(request):
            job = self.store.get_for_encounter(e["encounter_id"])
            items.append({**e, "queue_status": queue_status(job, e["note_status"]),
                          "error_class": job.error_class if job else None, "attempts": job.attempts if job else 0})
        return JSONResponse({"items": items})

    async def open(self, request: Request) -> JSONResponse:
        enc = await self._owned(request)
        if enc is None:
            return _error(403, "That encounter is not yours or does not exist.")
        try:
            bundle = await self._tool(request, "get_encounter_bundle", {"encounter_id": enc["encounter_id"]})
        except ToolCallError as exc:
            return _error(exc.status, str(exc))
        job = self.store.get_for_encounter(enc["encounter_id"])
        bundle["queue_status"] = queue_status(job, enc["note_status"])
        bundle["has_audio"] = encounter_files.exists(enc["encounter_id"], "audio")
        return JSONResponse(bundle)

    async def audio(self, request: Request) -> Response:
        enc = await self._owned(request)
        if enc is None:
            return _error(403, "That encounter is not yours or does not exist.")
        try:
            data = encounter_files.read(enc["encounter_id"], "audio")
        except encounter_files.EncounterFileError as exc:
            return _error(404, str(exc))
        content_type = encounter_files.meta(enc["encounter_id"], "audio").get("content_type", "audio/webm")
        return Response(data, media_type=content_type, headers={"Cache-Control": "no-store"})

    def routes(self) -> list[Route]:
        return [
            Route("/api/encounters", self.start, methods=["POST"]),
            Route("/api/encounters/{encounter_id}/audio", self.upload_audio, methods=["POST"]),
            Route("/api/encounters/{encounter_id}/audio", self.audio, methods=["GET"]),
            Route("/api/encounters/{encounter_id}/transcript", self.save_transcript, methods=["POST"]),
            Route("/api/encounters/{encounter_id}/end", self.end, methods=["POST"]),
            Route("/api/encounters/{encounter_id}/retry", self.retry, methods=["POST"]),
            Route("/api/encounters/{encounter_id}", self.open, methods=["GET"]),
            Route("/api/queue", self.queue, methods=["GET"]),
        ]


async def purge_expired_audio(call_tool, auth: BridgeAuth, retention_days: int, policy_ref: str,
                              logger: logging.Logger, now: datetime | None = None) -> list[str]:
    """Delete audio and working files for encounters signed more than retention_days ago.

    The deletion is recorded in the audit trail first (once per encounter), then
    the files are removed; a failed removal is retried on the next run.
    """
    now = now or datetime.now(timezone.utc)
    stored = set(encounter_files.stored_encounters())
    if not stored:
        return []
    listing = await call_tool("list_encounters", {"limit": 500})
    deleted = []
    for e in listing["encounters"]:
        eid, signed_at = e["encounter_id"], e.get("signed_at")
        if eid not in stored or not signed_at:
            continue
        if (now - datetime.fromisoformat(signed_at)).total_seconds() < retention_days * 86400:
            continue
        event = {"encounter_id": eid, "retention_days": retention_days, "policy_ref": policy_ref}
        token = auth.issue(attestation.WEB_APP_IDENTITY, "MeshMedic web app", attestation.AUDIO_DELETED, eid,
                           attestation.event_hash(event))
        try:
            await call_tool("record_audio_deletion", {**event, "attestation_token": token})
        except ToolCallError:
            log_event(logger, "error", TOOL_ERROR, new_correlation_id(), tool="purge_expired_audio",
                      encounter_id=eid, error_class="AuditLogWriteFailure")
            continue  # never delete without the audit record
        encounter_files.delete_encounter_files(eid)
        deleted.append(eid)
    return deleted


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
