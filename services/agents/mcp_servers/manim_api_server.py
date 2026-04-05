"""MCP server exposing ManimCE v0.20.1 API reference as tools.

Launched as a subprocess by Google ADK via stdio transport.
Loads pre-extracted API data from data/manim_api.json and exposes
three tools for agents to look up correct function signatures.
"""

import asyncio
import json
import pathlib

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ---------------------------------------------------------------------------
# Load and index the API data
# ---------------------------------------------------------------------------

DATA_PATH = pathlib.Path(__file__).parent / "data" / "manim_api.json"
DATA: list[dict] = json.loads(DATA_PATH.read_text())

# Index by lowercase name (skip constants entries that aren't real classes)
BY_NAME: dict[str, dict] = {}
for entry in DATA:
    key = entry["name"].lower()
    # If duplicate name, prefer classes/functions over constants
    if key not in BY_NAME or entry["type"] in ("class", "function"):
        BY_NAME[key] = entry

# Separate animation classes
ANIMATIONS: list[dict] = [
    e for e in DATA
    if "animation" in e.get("module", "") and e["type"] == "class"
]


def _format_entry(entry: dict) -> str:
    """Format a single API entry as readable text."""
    lines = []
    name = entry["name"]
    module = entry["module"]
    etype = entry["type"]

    if etype == "constants":
        lines.append(f"# {name} ({module})")
        lines.append(entry.get("docstring", ""))
        for p in entry.get("constructor_params", []):
            default = p.get("default", "")
            desc = p.get("description", "")
            extra = f"  # {desc}" if desc else ""
            lines.append(f"  {p['name']} = {default}{extra}")
        return "\n".join(lines)

    bases = ", ".join(entry.get("bases", []))
    sig = entry.get("signature", "()")
    lines.append(f"class {name}({bases}):" if etype == "class" else f"def {name}{sig}:")
    lines.append(f"  # Module: {module}")
    if entry.get("docstring"):
        lines.append(f'  """{entry["docstring"]}"""')
    if sig and etype == "class":
        lines.append(f"  def __init__{sig}: ...")

    # Constructor params
    params = entry.get("constructor_params", [])
    if params:
        lines.append("  # Parameters:")
        for p in params:
            ptype = p.get("type", "Any")
            default = p.get("default")
            desc = p.get("description", "")
            default_str = f" = {default}" if default is not None else ""
            desc_str = f"  # {desc}" if desc else ""
            lines.append(f"  #   {p['name']}: {ptype}{default_str}{desc_str}")

    # Methods
    methods = entry.get("methods", [])
    if methods:
        lines.append("  # Methods:")
        for m in methods:
            mdoc = m.get("docstring", "")
            lines.append(f"  def {m['name']}{m.get('signature', '()')}: ...  # {mdoc}")

    return "\n".join(lines)


def _search_score(query_tokens: list[str], entry: dict) -> int:
    """Score an entry by keyword overlap with query tokens."""
    text = " ".join([
        entry.get("name", ""),
        entry.get("module", ""),
        entry.get("docstring", ""),
    ]).lower()
    return sum(1 for t in query_tokens if t in text)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

app = Server("manim-api")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="lookup_manim_class",
            description=(
                "Look up a specific ManimCE class or function by name. "
                "Returns the full signature, constructor parameters with types/defaults, "
                "methods, base classes, and docstring. Use this BEFORE writing code "
                "to verify correct API usage."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "class_name": {
                        "type": "string",
                        "description": "The class or function name (e.g., 'Text', 'Axes', 'FadeIn', 'Create')",
                    }
                },
                "required": ["class_name"],
            },
        ),
        Tool(
            name="search_manim_api",
            description=(
                "Search the ManimCE API by keyword query. Returns top 5 matching "
                "classes/functions. Use when you're unsure which class to use."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g., 'plot function on axes', 'animate text appearing')",
                    }
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_manim_animations",
            description=(
                "List ALL available ManimCE animation classes with their signatures. "
                "Use this to find the correct animation name and parameters."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "lookup_manim_class":
        class_name = arguments.get("class_name", "").strip()
        key = class_name.lower()

        # Exact match
        entry = BY_NAME.get(key)

        # Fuzzy: try partial match
        if not entry:
            candidates = [e for k, e in BY_NAME.items() if key in k]
            if candidates:
                entry = candidates[0]

        if not entry:
            return [TextContent(
                type="text",
                text=f"No ManimCE class or function found matching '{class_name}'. "
                     f"Try search_manim_api to find similar classes.",
            )]

        return [TextContent(type="text", text=_format_entry(entry))]

    elif name == "search_manim_api":
        query = arguments.get("query", "").strip().lower()
        tokens = query.split()

        scored = [(entry, _search_score(tokens, entry)) for entry in DATA]
        scored = [(e, s) for e, s in scored if s > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:5]

        if not top:
            return [TextContent(
                type="text",
                text=f"No results found for '{query}'. Try different keywords.",
            )]

        results = []
        for entry, score in top:
            sig = entry.get("signature", "")
            doc = entry.get("docstring", "")[:100]
            results.append(
                f"- {entry['name']}{sig}  [{entry['module']}]\n  {doc}"
            )

        return [TextContent(type="text", text="\n\n".join(results))]

    elif name == "list_manim_animations":
        lines = ["# Available ManimCE Animation Classes\n"]
        # Group by module
        by_module: dict[str, list[dict]] = {}
        for anim in ANIMATIONS:
            mod = anim["module"]
            by_module.setdefault(mod, []).append(anim)

        for mod in sorted(by_module.keys()):
            lines.append(f"\n## {mod}")
            for a in sorted(by_module[mod], key=lambda x: x["name"]):
                sig = a.get("signature", "()")
                doc = a.get("docstring", "")[:80]
                lines.append(f"  {a['name']}{sig}")
                if doc:
                    lines.append(f"    # {doc}")

        return [TextContent(type="text", text="\n".join(lines))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
