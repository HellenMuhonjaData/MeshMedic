import asyncio
from types import SimpleNamespace

import progress_utils


def _run(coro):
    return asyncio.run(coro)


class _RecordingCtx:
    """Minimal stand-in for Context: just enough surface for emit_progress
    to read (request_context.meta) and call (report_progress), recording
    every call so tests can assert exactly what was sent."""

    def __init__(self, meta):
        self.request_context = SimpleNamespace(meta=meta)
        self.calls = []

    async def report_progress(self, progress, total, message):
        self.calls.append((progress, total, message))


def test_emit_progress_is_a_noop_when_meta_is_none():
    ctx = _RecordingCtx(meta=None)

    _run(progress_utils.emit_progress(ctx, 1, 10, "should never be sent"))

    assert ctx.calls == []


def test_emit_progress_is_a_noop_when_meta_has_no_progress_token():
    ctx = _RecordingCtx(meta={"some_other_key": "value"})

    _run(progress_utils.emit_progress(ctx, 1, 10, "should never be sent"))

    assert ctx.calls == []


def test_emit_progress_sends_with_a_real_total_when_a_token_is_present():
    ctx = _RecordingCtx(meta={"progress_token": "abc123"})

    _run(progress_utils.emit_progress(ctx, 3, 10, "3 of 10"))

    assert ctx.calls == [(3, 10, "3 of 10")]


def test_emit_progress_allows_a_none_total_when_the_amount_of_work_is_unknown():
    ctx = _RecordingCtx(meta={"progress_token": "abc123"})

    _run(progress_utils.emit_progress(ctx, 0, None, "in progress, no fixed step count"))

    assert ctx.calls == [(0, None, "in progress, no fixed step count")]
