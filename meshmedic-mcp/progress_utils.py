"""Shared progress-notification helper for meshmedic-mcp's long-running
tools (real external network calls, MCP sampling round-trips). One place
to read the progress token and skip emitting entirely when the client
didn't ask for updates, so every tool reports progress the same way
instead of each one re-implementing the token check.
"""

from mcp.server.mcpserver import Context


async def emit_progress(
    ctx: Context,
    progress: float,
    total: float | None,
    message: str,
) -> None:
    """Report progress to the client, but only if it asked.

    A progress token is opt-in per MCP request (carried in `_meta`) -- no
    token means no one is listening for updates, so this is a silent
    no-op rather than an error, and the caller behaves exactly as if this
    were never called.

    Pass `total=None` when the real amount of remaining work genuinely
    isn't known at the call site (a single opaque external call has no
    observable sub-steps) -- say so in `message` instead. Never invent a
    fraction against a total that isn't real.
    """
    meta = ctx.request_context.meta
    progress_token = meta.get("progress_token") if meta else None
    if progress_token is None:
        return
    await ctx.report_progress(progress, total, message)
