"""Care-gap prioritisation via MCP sampling.

Split out of server.py (Phase 0, no behaviour change).
"""

import time
from datetime import datetime, timezone
from typing import Annotated

from mcp.server.mcpserver import Context, Resolve, Sample
from mcp_types import CreateMessageResult, SamplingMessage, TextContent
from pydantic import Field

from app_core import (
    PRIORITIZE_GAPS_MAX_TOKENS,
    PRIORITIZE_GAPS_SYSTEM_PROMPT,
    _logger,
    mcp,
)
from audit_queries import _open_care_gaps_for_patient
from logging_utils import (
    SAMPLING_REQUEST_FINISHED,
    SAMPLING_REQUEST_STARTED,
    TOOL_COMPLETED,
    log_event,
    new_correlation_id,
)
from models import CareGapPriorityResult, OpenCareGapSummary
from progress_utils import emit_progress

# Bridges a sampling round-trip's start time (and the correlation id logged
# at its start) from the resolver that requests it to the tool body that
# receives the result -- the two are genuinely separate function
# invocations in this SDK's resolver model (see _list_open_gaps_resolver's
# docstring), so there is no other channel between them. Keyed by
# ctx.request_id (unique per in-flight tools/call), popped by the tool body
# on the normal path. If resolution fails before the tool body ever runs
# (e.g. the client's model refuses -- see prioritize_care_gaps's docstring
# for why that specific failure can't be caught here), the entry is never
# popped; this is a small, bounded leak on a rare path, not an unbounded one.
_sampling_calls: dict[str, tuple[str, float]] = {}


def _extract_ranking_text(content) -> str:
    """The client's sampled response as plain text. Sampling content can be
    text, image, or audio (SamplingContent); this tool only ever asks a
    text question, but a non-conformant client could still answer with a
    non-text block, so this returns a clear fallback string rather than
    crashing on a missing `.text` attribute -- never an empty answer."""
    if isinstance(content, TextContent):
        return content.text
    return f"[client returned a non-text response ({content.type}); no ranking text available]"


async def _list_open_gaps_resolver(
    patient_id: str, ctx: Context
) -> CreateMessageResult | Sample | None:
    """Resolver body for prioritize_care_gaps's `completion` parameter.

    Returning None means "don't attempt sampling at all" -- the framework
    never contacts the client in that case, only a real Sample marker
    does. Two things short-circuit to None: no open gaps (nothing to rank)
    and no declared sampling capability -- checking `ctx.client_capabilities`
    here, before ever building the marker, is what keeps a client that
    doesn't support sampling from ever seeing a MISSING_REQUIRED_CLIENT_CAPABILITY
    protocol error: that error is only raised once a Sample marker actually
    reaches the framework's fulfillment step, so simply never emitting one
    avoids it entirely and lets the tool body return a normal degraded
    result instead.

    This does NOT cover every failure mode by itself. If the client DOES
    declare sampling but then the specific request is refused or errors (a
    human declining an approval prompt, a timeout, a malformed response),
    that failure happens inside the framework's own fulfillment of the
    Sample marker -- entirely after this resolver has already returned and
    exited, so there is no code of mine positioned to catch it. It surfaces
    as a raised MCPError, which this SDK's tool-call handler re-raises
    rather than converting to a normal tool result (verified by reading
    mcp/server/mcpserver/server.py's _handle_call_tool: MCPError is
    special-cased to `raise` again, unlike ToolError/generic Exception,
    which do get turned into an is_error tool result). That path still
    doesn't crash the server process and doesn't return silence -- the
    calling model sees a clear MCP-level tool-call error -- but it does
    arrive as a protocol error rather than this tool's own
    CareGapPriorityResult(degraded=True, ...) shape. Doing better than that
    would mean bypassing this SDK's non-deprecated declarative sampling
    path for the deprecated imperative one (ctx.session.create_message,
    SEP-2577) just to get a try/except around the request -- a worse
    trade for a rarer failure mode, so this file doesn't make it.
    """
    gaps = _open_care_gaps_for_patient(patient_id)
    if not gaps:
        return None
    if ctx.client_capabilities is None or ctx.client_capabilities.sampling is None:
        return None

    correlation_id = new_correlation_id()
    log_event(
        _logger,
        "info",
        SAMPLING_REQUEST_STARTED,
        correlation_id,
        tool="prioritize_care_gaps",
        gap_count=len(gaps),
    )
    _sampling_calls[ctx.request_id] = (correlation_id, time.perf_counter())
    await emit_progress(
        ctx,
        0,
        None,
        f"Asking the client's model to prioritize {len(gaps)} open care gap(s) (single round-trip, no fixed step count)",
    )

    gap_list_text = "\n".join(
        f"- ({gap['gap_id']}) {gap['description']}" for gap in gaps
    )
    return Sample(
        messages=[
            SamplingMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"Here are this patient's currently open care gaps, each with an opaque id:\n\n{gap_list_text}",
                ),
            )
        ],
        max_tokens=PRIORITIZE_GAPS_MAX_TOKENS,
        system_prompt=PRIORITIZE_GAPS_SYSTEM_PROMPT,
    )


@mcp.tool()
async def prioritize_care_gaps(
    patient_id: Annotated[str, Field(min_length=1)],
    completion: Annotated[
        CreateMessageResult | None, Resolve(_list_open_gaps_resolver)
    ],
    *,
    ctx: Context,
) -> CareGapPriorityResult:
    """
    Ask the connected client's own model to reason about which of a
    patient's currently open care gaps (from identify_care_gaps, not yet
    closed by address_care_gap) are most clinically urgent to act on
    first, and why. This is judgment, not lookup -- urgency isn't stored
    anywhere in this system, so it can't be computed by sorting a field.

    This server fetches the open gaps itself, in plain Python, straight
    from the audit log (see _open_care_gaps_for_patient) -- no model call
    is involved in gathering that data. Only the actual prioritization
    reasoning goes through the client via MCP sampling. This server never
    names a model or holds an API key anywhere: sampling is answered by
    whatever LLM the connected client has configured, entirely on the
    client's side.

    A degraded result -- never a crash, never silent emptiness -- comes
    back with `degraded=true` and a `degraded_reason` in two cases: no
    open gaps exist for this patient (`"no_open_care_gaps"`), or the
    client hasn't declared MCP sampling support
    (`"sampling_not_supported_by_client"`). See
    _list_open_gaps_resolver's docstring for the one narrower failure mode
    (a client that declares sampling but then refuses or errors the
    specific request) that this tool cannot catch and convert into this
    same graceful shape -- it surfaces as an MCP tool-call error instead,
    which still isn't a crash or silence, just not this result type.

    `ranking_text` is the model's free-text response -- a ranked list with
    short reasons, per the system prompt -- not a decision. A clinician
    still reviews and acts on it, same as every other AI-drafted
    recommendation in this server.
    """
    gaps = _open_care_gaps_for_patient(patient_id)
    prioritized_at = datetime.now(timezone.utc).isoformat()
    open_gaps = [
        OpenCareGapSummary(gap_id=gap["gap_id"], description=gap["description"])
        for gap in gaps
    ]

    if not gaps:
        log_event(
            _logger,
            "info",
            TOOL_COMPLETED,
            new_correlation_id(),
            tool="prioritize_care_gaps",
            outcome="degraded",
            degraded_reason="no_open_care_gaps",
            gap_count=0,
        )
        return CareGapPriorityResult(
            patient_id=patient_id,
            gap_count=0,
            open_gaps=[],
            degraded=True,
            degraded_reason="no_open_care_gaps",
            ranking_text=None,
            prioritized_at=prioritized_at,
        )

    if completion is None:
        reason = (
            "sampling_not_supported_by_client"
            if ctx.client_capabilities is None
            or ctx.client_capabilities.sampling is None
            else "sampling_unavailable"
        )
        log_event(
            _logger,
            "warning",
            TOOL_COMPLETED,
            new_correlation_id(),
            tool="prioritize_care_gaps",
            outcome="degraded",
            degraded_reason=reason,
            gap_count=len(gaps),
        )
        return CareGapPriorityResult(
            patient_id=patient_id,
            gap_count=len(gaps),
            open_gaps=open_gaps,
            degraded=True,
            degraded_reason=reason,
            ranking_text=None,
            prioritized_at=prioritized_at,
        )

    started = _sampling_calls.pop(ctx.request_id, None)
    correlation_id, start_time = (
        started if started is not None else (new_correlation_id(), None)
    )
    duration_ms = (
        round((time.perf_counter() - start_time) * 1000, 1)
        if start_time is not None
        else None
    )
    log_event(
        _logger,
        "info",
        SAMPLING_REQUEST_FINISHED,
        correlation_id,
        tool="prioritize_care_gaps",
        outcome="success",
        duration_ms=duration_ms,
        gap_count=len(gaps),
    )
    await emit_progress(ctx, 1, None, "Received the client model's prioritization")
    return CareGapPriorityResult(
        patient_id=patient_id,
        gap_count=len(gaps),
        open_gaps=open_gaps,
        degraded=False,
        degraded_reason=None,
        ranking_text=_extract_ranking_text(completion.content),
        prioritized_at=prioritized_at,
    )
