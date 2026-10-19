"""Static scan for the Review Intelligence execution origin a call states (RC12-A).

Shared by the root Gate G guard and its hosted twin
(``enterprise/server/tests/test_gate_g_qualification.py`` loads this file by path),
so both halves see the same patterns.

What the scan resolves, per module: a direct call (``execute_structured(...)``,
``module.execute_structured(...)``); an import alias (``from ... import
execute_structured as run_model``); a plain re-binding (``run = execute_structured``,
``run = getattr(module, "execute_structured")``); ``functools.partial`` with the
origin bound at the partial or stated at the call; and the origin passed by name,
in a literal ``**{"origin": ...}`` / ``**dict(origin=...)`` mapping, or hidden in
any other ``**mapping`` (reported as ``<splat>``).

Out of scope, by construction: a callee reached through a *computed* name
(``getattr(module, "execute_" + "structured")``, ``vars(module)[built]``) or an
origin value computed at run time. A static scan cannot follow those; what still
holds there is the runtime rule that anything other than the exact ``human_ask``
string runs with the read-only profile.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

#: ``structured_executor`` is the injectable name AI review and rule checks call
#: execute_structured by.
ORIGIN_CALLEES = frozenset(
    {"execute_structured", "structured_executor", "answer_ask_execution", "answer_ask", "_execute_cli_structured"}
)
#: Origins a site can report besides a literal string.
PASSTHROUGH = "<passthrough>"  # origin=<an expression>: forwards a caller's value
SPLAT = "<splat>"  # **mapping that may carry the origin
UNSTATED = "<unstated>"


def _literal(node: ast.expr) -> str:
    return str(node.value) if isinstance(node, ast.Constant) else PASSTHROUGH


def _stated_origin(keywords: Iterable[ast.keyword]) -> str | None:
    """The origin these keywords state, or ``None`` when they state none."""

    origin: str | None = None
    for keyword in keywords:
        if keyword.arg == "origin":
            origin = _literal(keyword.value)
        elif keyword.arg is None:
            value = keyword.value
            if isinstance(value, ast.Dict):
                for key, item in zip(value.keys, value.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "origin":
                        origin = _literal(item)
                    elif not isinstance(key, ast.Constant):
                        origin = SPLAT  # {**other} or a computed key
            elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "dict":
                inner = _stated_origin(value.keywords)
                origin = inner if inner is not None else (SPLAT if value.args else origin)
            else:
                origin = SPLAT  # **kwargs: whatever the mapping holds
    return origin


def _getattr_name(node: ast.expr) -> str | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[1].value
    return None


def _resolve(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """The origin callee *node* names, through this module's aliases."""

    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute):
        return node.attr if node.attr in ORIGIN_CALLEES else None
    name = _getattr_name(node)
    return name if name in ORIGIN_CALLEES else None


def _is_partial(node: ast.expr) -> bool:
    func = node.func if isinstance(node, ast.Call) else None
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    return name == "partial"


def _targets(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        return [node.target.id]
    return []


def module_call_sites(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Every origin-carrying call in one module: (line, callee, origin)."""

    aliases = {name: name for name in ORIGIN_CALLEES}
    preset: dict[str, str] = {}  # partial-bound name -> the origin the partial binds
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in ORIGIN_CALLEES:
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            value = node.value
            callee = None
            bound: str | None = None
            if isinstance(value, ast.Call) and _is_partial(value) and value.args:
                callee = _resolve(value.args[0], aliases)
                bound = _stated_origin(value.keywords)
            elif isinstance(value, ast.Name | ast.Attribute) or _getattr_name(value) is not None:
                callee = _resolve(value, aliases)
            if callee is not None:
                for target in _targets(node):
                    aliases[target] = callee
                    if bound is not None:
                        preset[target] = bound

    sites: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_partial(node) and node.args:
            # The partial is a site only when it binds the origin itself; otherwise
            # the later call through it must state one.
            callee = _resolve(node.args[0], aliases)
            bound = _stated_origin(node.keywords)
            if callee is not None and bound is not None:
                sites.append((node.lineno, callee, bound))
            continue
        callee = _resolve(node.func, aliases)
        if callee is None:
            continue
        stated = _stated_origin(node.keywords)
        if stated is None and isinstance(node.func, ast.Name):
            stated = preset.get(node.func.id)
        sites.append((node.lineno, callee, UNSTATED if stated is None else stated))
    return sites


def origin_call_sites(roots: Iterable[Path]) -> list[tuple[str, int, str, str]]:
    """Every origin-carrying call under *roots*: (module, line, callee, origin)."""

    sites: list[tuple[str, int, str, str]] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            module = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            sites.extend((module, line, callee, origin) for line, callee, origin in module_call_sites(tree))
    return sites
