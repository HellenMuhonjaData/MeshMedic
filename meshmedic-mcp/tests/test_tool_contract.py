"""The MCP surface is a public contract: tool names, inputs and outputs must not drift.

tests/tool_contract_snapshot.json records every tool, prompt and resource
template with its schemas. Any change to that surface fails this test until
the snapshot is deliberately regenerated:

    uv run --no-sync python tests/test_tool_contract.py --update

Regenerate only when the change is intended, and review the snapshot diff
in the same commit (CLAUDE.md: breaking contract change = failing build).
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server

SNAPSHOT = Path(__file__).parent / "tool_contract_snapshot.json"


def _dump(obj):
    return obj.model_dump(mode="json", by_alias=True, exclude_none=True)


def current_surface() -> dict:
    async def collect():
        tools = await server.mcp.list_tools()
        prompts = await server.mcp.list_prompts()
        templates = await server.mcp.list_resource_templates()
        return {
            "tools": {
                t.name: {"input": _dump(t).get("inputSchema"), "output": _dump(t).get("outputSchema")}
                for t in tools
            },
            "prompts": sorted(p.name for p in prompts),
            "resource_templates": sorted(_dump(r).get("uriTemplate") for r in templates),
        }

    return asyncio.run(collect())


def test_mcp_surface_matches_snapshot():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(current_surface(), sort_keys=True))
    # Guard against a vacuous snapshot: every tool must have an input schema.
    assert expected["tools"] and all(s["input"] for s in expected["tools"].values())
    assert sorted(actual["tools"]) == sorted(expected["tools"]), "tool names changed"
    for name, schemas in expected["tools"].items():
        assert actual["tools"][name] == schemas, f"schema changed for tool {name!r}"
    assert actual["prompts"] == expected["prompts"]
    assert actual["resource_templates"] == expected["resource_templates"]


if __name__ == "__main__" and "--update" in sys.argv:
    SNAPSHOT.write_text(json.dumps(current_surface(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT}")
