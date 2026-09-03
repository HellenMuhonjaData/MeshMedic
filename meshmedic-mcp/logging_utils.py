"""Structured JSON log notifications for the meshmedic MCP server.

The installed mcp SDK (2.1.1) has deprecated the MCP protocol logging
capability (SEP-2577, 2026-07-28), and MCPServer never exposed a way to
declare it in the first place -- there is no supported path to send
notifications/message to the client in this build. Logs go to stderr
instead: stdout carries the MCP JSON-RPC stream for stdio transport, so
writing logs there would corrupt it, but stderr from a stdio-launched
server is exactly what an MCP client already captures.

One correlation_id per tool invocation ties every line from that call
together -- mint it with new_correlation_id() at the top of each tool
and pass it to every log_event() call the invocation makes, including
into any external-call helper it invokes (see epic_fhir_client.py).
"""

import json
import logging
import sys
import uuid
from typing import Any

_LOGGER_NAME = "meshmedic_mcp"

# Event names emitted by this module and its callers -- keep this list in
# sync with server.py / epic_fhir_client.py so the vocabulary stays stable.
TOOL_STARTED = "tool_started"
TOOL_COMPLETED = "tool_completed"
TOOL_ERROR = "tool_error"
ACCESS_DENIED = "access_denied"
EXTERNAL_CALL_STARTED = "external_call_started"
EXTERNAL_CALL_FINISHED = "external_call_finished"


def configure_json_logging(level: int = logging.INFO) -> logging.Logger:
    """Set up the shared logger once, at server construction time."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def log_event(
    logger: logging.Logger,
    level: str,
    event: str,
    correlation_id: str,
    **context: Any,
) -> None:
    """Emit one structured JSON log line for a single boundary.

    `context` must hold identifiers, counts, and durations only -- never an
    API key, connection string, credential, or raw patient record (name,
    DOB, MRN, note/transcript text). Callers are responsible for keeping
    what they pass here to that set.
    """
    payload = {
        "event": event,
        "correlation_id": correlation_id,
        "service": "meshmedic-mcp",
        **context,
    }
    getattr(logger, level)(json.dumps(payload))
