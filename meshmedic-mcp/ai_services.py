"""The two external AI calls MeshMedic makes: draft an encounter (Anthropic) and transcribe audio (OpenAI).

Shared by the interactive routes (/api/draft-note, /api/transcribe) and the
background worker. Every failure is classified so the worker knows what to
do with it:

* TransientAIError -- retry later (timeout, rate limit, network, 5xx, a
  malformed or cut-off draft).
* PermanentAIError -- retrying will not help; a person must act (rejected
  key, no credits, refusal, empty or unusable audio).

Both carry the user-facing message the routes already showed.

Timeouts are explicit (60 s per attempt); the SDKs' own retries are capped
at 2, so one call is bounded at roughly 3 minutes.
"""

import asyncio
from dataclasses import asdict

import anthropic
import openai

from note_drafting import (
    CONFIDENCE_INSTRUCTIONS,
    DRAFT_OUTPUT_SCHEMA,
    DraftContractError,
    NoteDraft,
    parse_draft,
)

TIMEOUT_SECONDS = 60.0
MAX_SDK_RETRIES = 2


class AIServiceError(Exception):
    error_class = "AIServiceError"


class TransientAIError(AIServiceError):
    error_class = "UpstreamUnavailable"


class PermanentAIError(AIServiceError):
    error_class = "UpstreamRejected"


def anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=MAX_SDK_RETRIES)


def openai_client() -> openai.OpenAI:
    return openai.OpenAI(timeout=TIMEOUT_SECONDS, max_retries=MAX_SDK_RETRIES)


def _draft_sync(client, model: str, max_tokens: int, system_prompt: str, transcript: str) -> tuple[NoteDraft, dict]:
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt + CONFIDENCE_INSTRUCTIONS,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": DRAFT_OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": transcript}],
        )
    except anthropic.AuthenticationError as exc:
        raise PermanentAIError("Anthropic API key was rejected (AuthenticationError).") from exc
    except anthropic.RateLimitError as exc:
        raise TransientAIError("Rate limited by the Anthropic API. Try again shortly.") from exc
    except anthropic.APIStatusError as exc:
        cls = TransientAIError if exc.status_code >= 500 else PermanentAIError
        raise cls(f"Anthropic API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:  # includes APITimeoutError
        raise TransientAIError("Could not reach the Anthropic API (network error).") from exc

    if response.stop_reason == "refusal":
        raise PermanentAIError("The AI declined to draft this note. Write it yourself instead.")
    if response.stop_reason == "max_tokens":
        raise TransientAIError("The AI draft was cut off before it finished. Try again.")
    raw = next((b.text for b in response.content if b.type == "text"), "")
    try:
        draft = parse_draft(raw)
    except DraftContractError as exc:
        raise TransientAIError(f"The AI draft was malformed ({exc}). Try again.") from exc
    usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    return draft, usage


async def draft_encounter(client, model: str, max_tokens: int, system_prompt: str, transcript: str) -> tuple[NoteDraft, dict]:
    """Draft note, codes, gaps, tests and referral from a transcript. Runs off the event loop."""
    return await asyncio.to_thread(_draft_sync, client, model, max_tokens, system_prompt, transcript)


def draft_to_dict(draft: NoteDraft) -> dict:
    return {**asdict(draft), "low_confidence": draft.low_confidence}


def _transcribe_sync(client, model: str, vocab_hint: str, audio: bytes, filename: str, content_type: str) -> str:
    if not audio:
        raise PermanentAIError("audio file is empty")
    try:
        result = client.audio.transcriptions.create(
            model=model, file=(filename, audio, content_type), prompt=vocab_hint
        )
    except openai.AuthenticationError as exc:
        raise PermanentAIError("OpenAI API key was rejected (AuthenticationError).") from exc
    except openai.RateLimitError as exc:
        # OpenAI returns 429 both for rate limiting and for an account with no
        # credits; only the body tells them apart, and only one is worth retrying.
        body = exc.body if isinstance(exc.body, dict) else {}
        if body.get("code") == "credit_balance_exhausted" or body.get("type") == "insufficient_quota":
            raise PermanentAIError("OpenAI account has no credits. Add billing at platform.openai.com, then try again.") from exc
        raise TransientAIError("Rate limited by the OpenAI API. Try again shortly.") from exc
    except openai.APIStatusError as exc:
        cls = TransientAIError if exc.status_code >= 500 else PermanentAIError
        raise cls(f"OpenAI API error ({exc.status_code}): {exc.message}") from exc
    except openai.APIConnectionError as exc:
        raise TransientAIError("Could not reach the OpenAI API (network error).") from exc
    text = (getattr(result, "text", "") or "").strip()
    if not text:
        raise PermanentAIError("No speech could be transcribed from this recording.")
    return text


async def transcribe(client, model: str, vocab_hint: str, audio: bytes, filename: str, content_type: str) -> str:
    return await asyncio.to_thread(_transcribe_sync, client, model, vocab_hint, audio, filename, content_type)
