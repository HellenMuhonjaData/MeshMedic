"""Roots enforcement for meshmedic-mcp: confine any caller-supplied
filesystem path to a directory the connected client actually declared.

Resolve first, compare second. The requested path is fully resolved to its
real, on-disk form -- this collapses ".." segments and follows any symlink
(or Windows junction) components -- BEFORE it is ever compared against an
allowed root. Comparing the raw, unresolved path against a root with a
plain string prefix check on str(path) is not a security boundary:

  - Sibling-directory collision: "/allowed-root-evil/x" starts with the
    string "/allowed-root" even though it is not a child of that directory
    at all -- a prefix check has no notion of a path *segment* boundary,
    only characters.
  - Unresolved "..": "allowed_root/../secret/passwd.txt" contains segments
    a string comparison never walks; only Path.resolve() collapses them to
    see where the path actually lands on disk (verified empirically: see
    PROGRESS.md for this change).
  - Symlink/junction escape: a link that lives inside an allowed root but
    points outside it still reads, as a string, like an in-root path -- the
    escape is only visible once the link target is followed, which
    resolve() does and a string check never can (a string check never
    touches the filesystem at all; confirmed with a real Windows junction
    during development of this module).
  - Case/separator drift: "C:\\Foo" vs "c:/foo" can make a genuinely
    in-bounds path fail a naive comparison, or an out-of-bounds one pass
    it, unless both sides are normalized through the same resolution.

So: resolve the requested path AND every declared root through the same
Path.resolve(), then use Path.is_relative_to() -- never a string op -- to
decide containment.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.request import url2pathname

from mcp_types import ListRootsResult, Root

from logging_utils import ACCESS_DENIED, log_event


def _root_to_path(root: Root) -> Path | None:
    """Convert one declared root's file:// URI to a local Path.

    Returns None for a root this process can't turn into a local path (no
    path component on the URI) -- such a root is excluded from the allowed
    set rather than crashing the whole check.
    """
    if root.uri.path is None:
        return None
    return Path(url2pathname(root.uri.path))


def resolve_within_roots(
    requested_path: str,
    roots: ListRootsResult,
    *,
    logger: logging.Logger,
    correlation_id: str,
    tool: str,
) -> Path | None:
    """Resolve `requested_path` to its real on-disk form and confirm it
    falls inside one of the client's currently declared roots.

    Returns the resolved absolute Path when allowed. Returns None -- after
    logging a warning-level access_denied event -- when the path resolves
    outside every declared root, when it can't be resolved at all (a
    malformed path), or when the client declared no roots (which denies
    everything by default rather than treating "no roots" as "no limit").
    """
    try:
        resolved: Path | None = Path(requested_path).expanduser().resolve(strict=False)
    except OSError:
        resolved = None

    allowed_roots = [
        p.resolve(strict=False) for p in (_root_to_path(r) for r in roots.roots) if p is not None
    ]

    if resolved is not None and any(resolved.is_relative_to(root) for root in allowed_roots):
        return resolved

    log_event(
        logger, "warning", ACCESS_DENIED, correlation_id,
        tool=tool, reason="outside_declared_roots", requested_path=requested_path,
        declared_root_count=len(allowed_roots), error_class="AccessDenied",
    )
    return None
