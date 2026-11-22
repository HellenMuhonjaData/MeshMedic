"""Background processing of finished consultations (Phase 2, STORY-015).

Stages, in order, for one encounter:
    transcribe -> draft -> record -> check
* transcribe: audio -> transcript (skipped if a typed transcript was saved)
* draft:      transcript -> AI draft (note, codes, care gaps, tests, referral)
* record:     record the AI's versions through the MCP tools, filed under
              the encounter: generate_encounter_note, suggest_codes,
              record_ai_suggestions. Nothing is approved, signed, ordered or
              sent -- the worker has no attestation, so it cannot.
* check:      mark the encounter ready for review

Every stage is safe to repeat. Stage outputs are kept as encrypted working
files, so a stage that already finished is skipped on resume, and the
record stage passes duplicate-prevention keys (`<encounter>:note`,
`<encounter>:codes`) so a retry returns what it already created.

Failures (answers to the four failure questions):
1. Transient (timeout, rate limit, network, 5xx, malformed AI output) ->
   retried with backoff (15 s, 60 s), at most 3 attempts in total.
2. Permanent (rejected key, refusal, no audio, wrong patient) -> no retry.
3. After the last attempt, or a permanent error, the encounter is marked
   needs_attention: shown in the review queue with Retry and with the audio
   and any transcript kept, so the clinician can retry or document by hand.
4. Not handled here: a clinic offline all day (jobs simply wait), audio too
   poor to transcribe meaningfully (it drafts with low confidence instead).
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import ai_services
import encounter_files
import job_store
from logging_utils import TOOL_COMPLETED, TOOL_ERROR, log_event, new_correlation_id

ToolCaller = Callable[[str, dict], Awaitable[dict]]
Transcriber = Callable[[bytes, str, str], Awaitable[str]]
Drafter = Callable[[str], Awaitable[dict]]


class PermanentJobError(Exception):
    """Retrying will not help."""

    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


class EncounterWorker:
    def __init__(self, store: job_store.JobStore, call_tool: ToolCaller, transcribe: Transcriber,
                 draft: Drafter, worker_id: str, logger: logging.Logger, tool_error_type: type) -> None:
        self.store = store
        self._call_tool = call_tool
        self._transcribe = transcribe
        self._draft = draft
        self.worker_id = worker_id
        self._logger = logger
        self._tool_error = tool_error_type  # the web app's ToolCallError (has .status)

    async def run_once(self) -> bool:
        """Process the next due job, if any. True if a job was processed."""
        job = self.store.claim(self.worker_id)
        if job is None:
            return False
        await self._process(job)
        return True

    async def run_forever(self, stop: asyncio.Event, poll_seconds: float = 2.0) -> None:
        while not stop.is_set():
            worked = await self.run_once()
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
                except asyncio.TimeoutError:
                    pass  # nothing due; poll again

    async def _process(self, job: job_store.Job) -> None:
        correlation_id = new_correlation_id()
        enc = job.encounter_id
        try:
            for stage in job_store.STAGES[job_store.STAGES.index(job.stage):]:
                self.store.advance(job.job_id, stage)
                self.store.heartbeat(job.job_id, self.worker_id)
                await getattr(self, f"_stage_{stage}")(enc)
            self.store.finish(job.job_id)
            log_event(self._logger, "info", TOOL_COMPLETED, correlation_id, tool="encounter_worker",
                      encounter_id=enc, outcome="success")
            return
        except PermanentJobError as exc:
            error_class, retryable = exc.error_class, False
        except ai_services.PermanentAIError as exc:
            error_class, retryable = exc.error_class, False
        except ai_services.TransientAIError as exc:
            error_class, retryable = exc.error_class, True
        except encounter_files.EncounterFileError:
            error_class, retryable = "EncounterFileError", False
        except Exception as exc:  # noqa: BLE001 -- the worker must never die; logged and retried, never swallowed
            if isinstance(exc, self._tool_error):
                error_class, retryable = "ToolCallFailed", getattr(exc, "status", 500) >= 500
            else:
                error_class, retryable = type(exc).__name__, True
        failed = self.store.fail(job.job_id, error_class, retryable)
        log_event(self._logger, "error", TOOL_ERROR, correlation_id, tool="encounter_worker", encounter_id=enc,
                  error_class=error_class, status=failed.status, attempts=failed.attempts)

    # ---- stages ----
    async def _stage_transcribe(self, enc: str) -> None:
        if encounter_files.exists(enc, "transcript"):
            return
        if not encounter_files.exists(enc, "audio"):
            raise PermanentJobError("NoAudio", "no recording or transcript was saved for this encounter")
        meta = encounter_files.meta(enc, "audio")
        text = await self._transcribe(
            encounter_files.read(enc, "audio"), meta.get("filename", "audio.webm"), meta.get("content_type", "audio/webm")
        )
        encounter_files.write(enc, "transcript", text.encode("utf-8"))

    async def _stage_draft(self, enc: str) -> None:
        if encounter_files.exists(enc, "draft"):
            return
        transcript = encounter_files.read(enc, "transcript").decode("utf-8")
        draft = await self._draft(transcript)
        encounter_files.write(enc, "draft", json.dumps(draft).encode("utf-8"))

    async def _stage_record(self, enc: str) -> None:
        context = json.loads(encounter_files.read(enc, "context"))
        transcript = encounter_files.read(enc, "transcript").decode("utf-8")
        draft = json.loads(encounter_files.read(enc, "draft"))
        try:
            note = await self._call_tool("generate_encounter_note", {
                "ehr_system": context["ehr_system"], "patient_id": context["patient_id"], "transcript": transcript,
                "note_text": draft["note_text"], "confidence": draft["confidence"],
                "confidence_reason": draft.get("confidence_reason") or None, "source": "ai",
                "encounter_id": enc, "idempotency_key": f"{enc}:note",
            })
            if draft.get("codes"):
                await self._call_tool("suggest_codes", {
                    "note_id": note["note_id"], "codes": draft["codes"], "confidence": draft["codes_confidence"],
                    "confidence_reason": draft.get("codes_confidence_reason") or None, "source": "ai",
                    "idempotency_key": f"{enc}:codes",
                })
            suggestions = {"note_id": note["note_id"], "care_gaps": draft.get("care_gaps") or [],
                           "test_orders": draft.get("test_orders") or []}
            if draft.get("referral"):
                suggestions["referral"] = draft["referral"]
            await self._call_tool("record_ai_suggestions", suggestions)
        except self._tool_error as exc:
            if getattr(exc, "status", 500) < 500:
                raise PermanentJobError("ToolRefused", str(exc)) from exc
            raise

    async def _stage_check(self, enc: str) -> None:
        # Phase 3 adds attention items here; for now a processed encounter is ready for review.
        return
