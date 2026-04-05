#!/usr/bin/env python3
"""Extract ManimCE API definitions via introspection.

Run this inside the renderer container (where manim is installed) to
regenerate the static API reference JSON used by the MCP server.

Usage:
    docker-compose run --rm renderer python /app/scripts/extract_manim_api.py

Output is written to stdout as JSON. Redirect to the data file:
    ... > services/agents/mcp_servers/data/manim_api.json
"""

import importlib
import inspect
import json
import re
import sys

# Modules to introspect — covers the most commonly used Manim API surface
MODULES = [
    "manim.animation.creation",
    "manim.animation.fading",
    "manim.animation.transform",
    "manim.animation.transform_matching_parts",
    "manim.animation.indication",
    "manim.animation.composition",
    "manim.animation.growing",
    "manim.animation.rotation",
    "manim.animation.movement",
    "manim.animation.updaters",
    "manim.scene.scene",
    "manim.mobject.mobject",
    "manim.mobject.types.vectorized_mobject",
    "manim.mobject.geometry.arc",
    "manim.mobject.geometry.line",
    "manim.mobject.geometry.polygram",
    "manim.mobject.geometry.shape_matchers",
    "manim.mobject.geometry.labeled",
    "manim.mobject.text.text_mobject",
    "manim.mobject.text.tex_mobject",
    "manim.mobject.graphing.coordinate_systems",
    "manim.mobject.graphing.number_line",
    "manim.mobject.graph",
    "manim.mobject.matrix",
    "manim.mobject.table",
    "manim.mobject.value_tracker",
    "manim.mobject.svg.brace",
    "manim.mobject.frame",
    "manim.mobject.vector_field",
]


def extract_params(cls_or_func):
    """Extract constructor/function parameters."""
    try:
        sig = inspect.signature(cls_or_func)
    except (ValueError, TypeError):
        return [], ""

    params = []
    for name, param in sig.parameters.items():
        if name in ("self", "_mobject", "use_override"):
            continue
        p = {"name": name, "type": "Any", "default": None}
        if param.annotation is not inspect.Parameter.empty:
            p["type"] = str(param.annotation)
        if param.default is not inspect.Parameter.empty:
            try:
                raw = repr(param.default)
                # Strip memory addresses like <function linear at 0x7f...>
                raw = re.sub(r' at 0x[0-9a-fA-F]+', '', raw)
                p["default"] = raw
            except Exception:
                p["default"] = str(param.default)
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            p["name"] = f"*{name}"
        elif param.kind == inspect.Parameter.VAR_KEYWORD:
            p["name"] = f"**{name}"
        params.append(p)

    return params, str(sig)


def extract_methods(cls):
    """Extract public methods defined directly on this class (not inherited)."""
    methods = []
    # Only methods from this class's own __dict__, not inherited ones
    own_names = set(cls.__dict__.keys())
    for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
        if name.startswith("_") or name not in own_names:
            continue
        try:
            sig = str(inspect.signature(method))
        except (ValueError, TypeError):
            sig = "(...)"
        doc = inspect.getdoc(method) or ""
        methods.append({
            "name": name,
            "signature": sig,
            "docstring": doc[:200],
        })
    return methods[:25]  # Cap to avoid bloat


def extract_entry(name, obj, module_name):
    """Extract a single class or function entry."""
    entry = {
        "module": module_name,
        "name": name,
        "type": "class" if inspect.isclass(obj) else "function",
        "bases": [],
        "signature": "",
        "docstring": (inspect.getdoc(obj) or "")[:300],
        "constructor_params": [],
        "methods": [],
    }

    if inspect.isclass(obj):
        entry["bases"] = [b.__name__ for b in obj.__mro__[1:4] if b.__name__ != "object"]
        init = getattr(obj, "__init__", None)
        if init:
            params, sig = extract_params(init)
            entry["constructor_params"] = params
            entry["signature"] = sig.replace("(self, ", "(").replace("(self)", "()")
        entry["methods"] = extract_methods(obj)
    else:
        params, sig = extract_params(obj)
        entry["constructor_params"] = params
        entry["signature"] = sig

    return entry


def main():
    results = []
    seen = set()

    for mod_name in MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError as e:
            print(f"Warning: could not import {mod_name}: {e}", file=sys.stderr)
            continue

        for name, obj in inspect.getmembers(mod):
            if name.startswith("_"):
                continue
            if not (inspect.isclass(obj) or inspect.isfunction(obj)):
                continue
            # Only include items defined in this module (not re-exports)
            obj_module = getattr(obj, "__module__", "")
            if not obj_module.startswith(mod_name.rsplit(".", 1)[0]):
                continue
            key = f"{obj_module}.{name}"
            if key in seen:
                continue
            seen.add(key)

            results.append(extract_entry(name, obj, obj_module))

    json.dump(results, sys.stdout, indent=2, default=str)
    print(f"\nExtracted {len(results)} entries", file=sys.stderr)


if __name__ == "__main__":
    main()
