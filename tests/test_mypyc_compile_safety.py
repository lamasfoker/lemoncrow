"""Static safety net for the mypyc-compiled release build.

``hatch_build.py`` compiles nearly all of ``src/lemoncrow/`` to mypyc ``.so``
extensions for the shipped wheel (editable installs / ``uv run`` stay pure
Python -- see ``CustomBuildHook.initialize``'s early return on
``version == "editable"``). A compiled ("native") class instance behaves
differently from a plain Python object in ways an ordinary ``pytest -q`` run
never exercises, because the whole suite normally runs against the
uncompiled ``.py`` source:

* mypyc-native instances have **no** ``__dict__``. ``self.__dict__``
  access (bare, ``.get``, ``.setdefault``, ``[...]``) that works fine
  interpreted raises ``AttributeError`` only in the compiled build. This bit
  ``CodeContextEngine`` for real: ``self.__dict__.setdefault("_hef_anchor_cache", {})``
  et al. worked under ``uv run`` but raised
  ``AttributeError: 'CodeContextEngine' object has no attribute '__dict__'``
  from the shipped ``lc`` MCP server's ``code_search`` tool. Fixed by
  declaring the caches as real ``__init__``-assigned instance attributes
  instead of lazy ``self.__dict__`` entries.
* mypyc-native instances also have **no** ``__weakref__`` slot --
  ``weakref.ref(self, ...)`` / ``weakref.finalize(self, ...)`` raise
  ``TypeError: cannot create weak reference``. See
  ``lemoncrow/core/foundation/weakref_token.py``'s ``WeakRefToken`` sentinel,
  which every native class must route through instead.

Building the real wheel to catch these takes minutes (see
``tests/gateway/test_mcp_compiled_so.py``, which does exactly that for a
sibling mypyc gotcha). This module is the cheap, always-on guard: no compile
step, just an AST scan of every file mypyc will actually compile in the next
release build. It exists so a reappearance of either pattern fails an
ordinary ``pytest -q`` run immediately, long before a release build.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_HATCH_BUILD = REPO_ROOT / "hatch_build.py"

# hatch_build.py imports `hatchling` only to subclass BuildHookInterface for the
# real build hook -- irrelevant to `_find_compilable`, which this module only
# needs for its module-level regexes/constants. hatchling is a build-backend
# dependency (present under `uv build`'s isolated env), not a dev/test
# dependency, so `uv run pytest` doesn't have it installed. Stub the import
# rather than pulling in a real build dependency just to read a file list.
if "hatchling.builders.hooks.plugin.interface" not in sys.modules:
    _interface_mod = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    _interface_mod.BuildHookInterface = type("BuildHookInterface", (), {})  # type: ignore[attr-defined]
    for _name in (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
    ):
        sys.modules.setdefault(_name, types.ModuleType(_name))
    sys.modules["hatchling.builders.hooks.plugin.interface"] = _interface_mod

_spec = importlib.util.spec_from_file_location("_hatch_build_under_test", _HATCH_BUILD)
assert _spec and _spec.loader
hatch_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hatch_build)


def _compiled_files() -> list[Path]:
    """Every file the next release wheel will mypyc-compile, per hatch_build.py."""
    src_dir = REPO_ROOT / "src"
    lemoncrow_src = src_dir / "lemoncrow"
    rels = hatch_build._find_compilable(lemoncrow_src, src_dir)
    return [src_dir / rel for rel in rels]


def test_async_generators_are_excluded_from_mypyc_release_build() -> None:
    assert hatch_build._contains_async_generator("async def stream():\n    yield 1\n") is True
    assert hatch_build._contains_async_generator("async def value():\n    return 1\n") is False
    compiled = {path.relative_to(REPO_ROOT / "src").as_posix() for path in _compiled_files()}
    assert "lemoncrow/pro/capabilities/review/state_events.py" not in compiled


def _is_self(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "self"


def _find_self_dict_access(tree: ast.AST) -> list[int]:
    """Line numbers of any ``self.__dict__`` attribute access in *tree*.

    Matches regardless of what wraps it (``self.__dict__.get(...)``,
    ``self.__dict__[...]``, ``self.__dict__.setdefault(...)``, bare
    ``self.__dict__``) because ``ast.Attribute(value=self, attr="__dict__")``
    is the shared inner node in every one of those forms.
    """
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "__dict__" and _is_self(node.value)
        }
    )


def _find_weakref_on_self(tree: ast.AST) -> list[int]:
    """Line numbers of ``weakref.ref(self, ...)`` / ``weakref.finalize(self, ...)``.

    Covers both ``import weakref; weakref.ref(self)`` and
    ``from weakref import ref, finalize; ref(self)`` call shapes.
    """
    hits: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args or not _is_self(node.args[0]):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name in {"ref", "finalize"} and (
            isinstance(func, ast.Name)
            or (isinstance(func, ast.Attribute) and getattr(func.value, "id", None) == "weakref")
        ):
            hits.add(node.lineno)
    return sorted(hits)


def test_no_self_dict_access_in_compiled_modules() -> None:
    """Regression guard: mypyc-native instances have no ``__dict__``.

    ``self.__dict__`` (get/setdefault/subscript/bare) must never reappear in a
    module mypyc will compile -- see ``CodeContextEngine``'s
    ``_hef_anchor_cache``/``_line_fts_total_cache``/``_line_fts_df_cache``, now
    real ``__init__`` attributes instead of ``self.__dict__`` lazy entries.
    """
    offenders: dict[str, list[int]] = {}
    for path in _compiled_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = _find_self_dict_access(tree)
        if lines:
            offenders[str(path.relative_to(REPO_ROOT))] = lines
    assert not offenders, (
        "self.__dict__ access found in module(s) the release wheel compiles to a "
        "mypyc-native class with no __dict__ -- use a real __init__-declared "
        f"instance attribute instead:\n{offenders}"
    )


def test_no_weakref_on_self_in_compiled_modules() -> None:
    """Regression guard: mypyc-native instances have no ``__weakref__`` slot.

    ``weakref.ref(self, ...)`` / ``weakref.finalize(self, ...)`` raise
    ``TypeError: cannot create weak reference`` in the compiled build. Route
    through ``lemoncrow.core.foundation.weakref_token.WeakRefToken`` (a
    non-native sentinel) instead -- see ``CodeContextEngine._gc_sentinel``.
    """
    offenders: dict[str, list[int]] = {}
    for path in _compiled_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = _find_weakref_on_self(tree)
        if lines:
            offenders[str(path.relative_to(REPO_ROOT))] = lines
    assert not offenders, (
        "weakref.ref(self, ...)/weakref.finalize(self, ...) found in module(s) "
        "the release wheel compiles to a mypyc-native class with no __weakref__ "
        f"slot -- route through WeakRefToken instead:\n{offenders}"
    )


def test_review_store_compiles_with_its_sqlite_base() -> None:
    """No interpreted ReviewStore may inherit the native SqliteTableStore."""
    compiled = {str(path.relative_to(REPO_ROOT / "src")) for path in _compiled_files()}
    assert "lemoncrow/core/foundation/sqlite_base.py" in compiled
    assert "lemoncrow/pro/capabilities/review/store.py" in compiled
    assert "lemoncrow/pro/capabilities/review/errors.py" not in compiled
    assert "lemoncrow/pro/capabilities/review/state_events.py" not in compiled


def test_no_async_generators_in_compiled_modules() -> None:
    """mypyc does not support async generators (async def with yield)."""
    offenders: list[str] = []
    for path in _compiled_files():
        text = path.read_text(encoding="utf-8")
        if hatch_build._has_async_generator(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "async generator found in module(s) marked for mypyc compilation; "
        f"mypyc cannot compile async generators:\n{offenders}"
    )


def test_a_class_reaching_an_unsubclassable_builtin_through_an_in_repo_base_stays_interpreted(
    tmp_path: Path,
) -> None:
    """mypyc rejects any native class whose MRO holds a builtin it cannot subclass.

    The direct regex sees only ``class E(RuntimeError)``; ``class F(E)`` one
    module over broke the compiled release build (``intelligence_runtime.py``).
    ValueError lineage stays compiled: mypyc subclasses that natively.
    """
    # Not named lemoncrow: the public-source boundary audit reads these embedded imports.
    package = tmp_path / "src" / "fixture"
    package.mkdir(parents=True)
    modules = {
        "errors.py": "class E(RuntimeError):\n    pass\n\nclass V(ValueError):\n    pass\n",
        "refused.py": "from fixture import errors\n\nclass F(errors.E):\n    pass\n",
        "deeper.py": "from fixture.refused import F\n\nclass G(F):\n    pass\n",
        # Scanned before the bases it reaches through: only a fixpoint finds it.
        "chain.py": "from fixture.deeper import G\n\nclass H(G):\n    pass\n",
        "value.py": "from fixture.errors import V\n\nclass W(V):\n    pass\n",
        "plain.py": "class P:\n    pass\n",
    }
    for name, text in modules.items():
        (package / name).write_text(text)
    compiled = hatch_build._find_compilable(package, tmp_path / "src")
    assert compiled == ["fixture/plain.py", "fixture/value.py"]
    real = {path.relative_to(REPO_ROOT / "src").as_posix() for path in _compiled_files()}
    assert "lemoncrow/pro/capabilities/review/intelligence_runtime.py" not in real


def test_a_membership_test_against_a_starred_tuple_or_list_literal_stays_interpreted(tmp_path: Path) -> None:
    """mypyc's ``x in (a, b)`` fast path is an internal error on a starred item.

    It broke the compiled release build (``escalation.py``: ``value in (*allowed, "off")``).
    A set literal and a loop over a starred tuple take other paths and compile.
    """
    package = tmp_path / "src" / "lemoncrow"
    package.mkdir(parents=True)
    modules = {
        "tuple_in.py": "XS = ('a',)\n\ndef f(x: str) -> bool:\n    return x in (*XS, 'off')\n",
        "list_not_in.py": "XS = ['a']\n\ndef f(x: str) -> bool:\n    return x not in [\n        *XS,\n    ]\n",
        "set_in.py": "XS = ('a',)\n\ndef f(x: str) -> bool:\n    return x in {*XS, 'off'}\n",
        "loop.py": "XS = ('a',)\n\ndef f() -> list[str]:\n    return [x for x in (*XS, 'off')]\n",
        "plain_in.py": "XS = ('a',)\n\ndef f(x: str) -> bool:\n    return x in ('off', 'on') or x in {*XS}\n",
    }
    for name, text in modules.items():
        (package / name).write_text(text)
    compiled = hatch_build._find_compilable(package, tmp_path / "src")
    assert compiled == ["lemoncrow/loop.py", "lemoncrow/plain_in.py", "lemoncrow/set_in.py"]


def test_a_module_level_getattr_stays_interpreted_even_in_pro(tmp_path: Path) -> None:
    """A mypyc 2.2 module defining PEP 562 ``__getattr__`` segfaults on import.

    ``__init__.py`` outside pro/ was skipped by name, but pro/ is exempt from that
    list, so the lazy ``tool_supervision`` and ``code_context`` packages compiled
    and the candidate crashed importing Review (``build_local_server``). A class
    ``__getattr__`` method compiles and imports cleanly.
    """
    package = tmp_path / "src" / "lemoncrow"
    (package / "pro" / "lazy").mkdir(parents=True)
    modules = {
        "pro/lazy/__init__.py": "from typing import Any\n\ndef __getattr__(name: str) -> Any:\n    raise AttributeError(name)\n",
        "pro/facade.py": "def __getattr__(name):\n    raise AttributeError(name)\n",
        "pro/method.py": "class C:\n    def __getattr__(self, name: str) -> str:\n        return name\n",
        "pro/nested.py": "def f() -> None:\n    def __getattr__(name: str) -> None:\n        pass\n",
    }
    for name, text in modules.items():
        (package / name).write_text(text)
    compiled = hatch_build._find_compilable(package, tmp_path / "src")
    assert compiled == ["lemoncrow/pro/method.py", "lemoncrow/pro/nested.py"]
    real = {path.relative_to(REPO_ROOT / "src").as_posix() for path in _compiled_files()}
    assert not real & {
        "lemoncrow/pro/capabilities/tool_supervision/__init__.py",
        "lemoncrow/pro/capabilities/code_context/__init__.py",
    }


def test_a_dataclass_declaring_a_classvar_stays_interpreted(tmp_path: Path) -> None:
    """mypyc 2.2 makes a dataclass's ``ClassVar`` an init field.

    Ahead of a required field that is a TypeError on import (``differential.py``:
    ``__test__: ClassVar[bool] = False``, which failed every compiled Review
    request); after one, ``fields()`` and ``__init__`` silently grow it. A
    ``ClassVar`` on a plain class compiles and imports cleanly.
    """
    package = tmp_path / "src" / "lemoncrow"
    package.mkdir(parents=True)
    head = "from dataclasses import dataclass\nimport dataclasses\nfrom typing import ClassVar\nimport typing\n\n"
    modules = {
        "first.py": head
        + "@dataclass(frozen=True, slots=True)\nclass T:\n    __test__: ClassVar[bool] = False\n    id: str\n",
        "after.py": head + "@dataclasses.dataclass\nclass T:\n    id: str\n    kind: typing.ClassVar[str] = 'x'\n",
        "native.py": head + "class N:\n    marker: ClassVar[bool] = False\n\n@dataclass\nclass D:\n    id: str\n",
    }
    for name, text in modules.items():
        (package / name).write_text(text)
    compiled = hatch_build._find_compilable(package, tmp_path / "src")
    assert compiled == ["lemoncrow/native.py"]
    real = {path.relative_to(REPO_ROOT / "src").as_posix() for path in _compiled_files()}
    assert "lemoncrow/pro/capabilities/review/differential.py" not in real


def test_an_ellipsis_parameter_default_stays_interpreted(tmp_path: Path) -> None:
    """mypy accepts ``max_bytes: int = ...`` in a Protocol stub; mypyc checks it on import.

    The compiled ``hermetic_python.py`` raised "int object expected; got ellipsis"
    the moment it was imported.
    """
    package = tmp_path / "src" / "lemoncrow"
    package.mkdir(parents=True)
    modules = {
        "keyword.py": "from typing import Protocol\n\nclass F(Protocol):\n    def f(self, *, n: int = ...) -> int: ...\n",
        "positional.py": "from typing import Protocol\n\nclass F(Protocol):\n    async def f(self, n: int = ...) -> int: ...\n",
        "body.py": "from typing import Protocol\n\nclass F(Protocol):\n    def f(self, n: int = 5) -> int: ...\n",
    }
    for name, text in modules.items():
        (package / name).write_text(text)
    compiled = hatch_build._find_compilable(package, tmp_path / "src")
    assert compiled == ["lemoncrow/body.py"]
    real = {path.relative_to(REPO_ROOT / "src").as_posix() for path in _compiled_files()}
    assert "lemoncrow/pro/capabilities/review/hermetic_python.py" not in real


def test_a_module_whose_source_is_read_with_getsource_stays_interpreted(tmp_path: Path) -> None:
    """A compiled module has no source: ``inspect.getsource(module)`` raises OSError.

    ``shipped_artifacts.py`` ships ``artifact_inspection``'s source into its
    sandbox; compiled, four Review modules failed to import.
    """
    # Not named lemoncrow: the public-source boundary audit reads these embedded imports.
    package = tmp_path / "src" / "fixture"
    (package / "pkg").mkdir(parents=True)
    modules = {
        "pkg/__init__.py": "",
        "pkg/shipped.py": "X = 1\n",
        "pkg/aliased.py": "X = 1\n",
        "pkg/called.py": "X = 1\n",
        "reader.py": (
            "import inspect\nfrom inspect import getsource\n"
            "from fixture.pkg import shipped\nimport fixture.pkg.aliased as aliased\nfrom fixture.pkg import called\n\n"
            "A = inspect.getsource(shipped)\nB = getsource(aliased)\nC = called.X\n"
        ),
    }
    for name, text in modules.items():
        (package / name).write_text(text)
    compiled = hatch_build._find_compilable(package, tmp_path / "src")
    assert compiled == ["fixture/pkg/called.py", "fixture/reader.py"]
    real = {path.relative_to(REPO_ROOT / "src").as_posix() for path in _compiled_files()}
    assert "lemoncrow/pro/capabilities/review/artifact_inspection.py" not in real


def test_a_fastapi_module_stays_interpreted_even_without_di_defaults(tmp_path: Path) -> None:
    """FastAPI reads every endpoint's signature; a mypyc 2.2 function has none.

    The compiled ``action_approval_api.py`` route closure failed every Review
    request with "no signature found for builtin" (elsewhere ``inspect.signature``
    returns ``()``, dropping every parameter). Its handlers use no ``Depends()``.
    """
    package = tmp_path / "src" / "lemoncrow"
    package.mkdir(parents=True)
    route = "\n\ndef register(router: APIRouter) -> None:\n    @router.get('/x')\n    async def x(n: int) -> dict[str, int]:\n        return {'n': n}\n"
    modules = {
        "routes.py": "from fastapi import APIRouter" + route,
        "aliased.py": "import fastapi\nAPIRouter = fastapi.APIRouter" + route,
        "plain.py": "APIRouter = object" + route,
    }
    for name, text in modules.items():
        (package / name).write_text(text)
    compiled = hatch_build._find_compilable(package, tmp_path / "src")
    assert compiled == ["lemoncrow/plain.py"]
    real = {path.relative_to(REPO_ROOT / "src").as_posix() for path in _compiled_files()}
    assert "lemoncrow/pro/capabilities/review/action_approval_api.py" not in real


def test_a_module_whose_functions_a_decorator_introspects_stays_interpreted(tmp_path: Path) -> None:
    """A compiled function keeps its parameter names, never its annotations.

    ``@mcp_tool`` builds each tool's input schema and coerces stringified arguments from the
    decorated function's type hints. With the ``tools_*.py`` modules compiled, every parameter
    was ``Any``: ``"20"`` reached an ``int`` parameter and the compiled handler raised
    ``TypeError: int object expected; got str``.
    """
    package = tmp_path / "src" / "fixture"
    package.mkdir(parents=True)
    modules = {
        "registry.py": (
            "import inspect\nfrom typing import get_type_hints\n\n"
            "def tool(name=None):\n    def wrap(func):\n        get_type_hints(func)\n        return func\n    return wrap\n\n"
            "def route(func):\n    inspect.signature(func)\n    return func\n"
        ),
        "called.py": "from fixture.registry import tool\n\n@tool(name='x')\ndef read(n: int = 20) -> int:\n    return n\n",
        "bare.py": "from fixture import registry\n\n@registry.route\ndef read(n: int = 20) -> int:\n    return n\n",
        "plain.py": "import functools\n\n@functools.cache\ndef read(n: int = 20) -> int:\n    return n\n",
    }
    for name, text in modules.items():
        (package / name).write_text(text)
    compiled = hatch_build._find_compilable(package, tmp_path / "src")
    assert compiled == ["fixture/plain.py", "fixture/registry.py"]
    real = {path.relative_to(REPO_ROOT / "src").as_posix() for path in _compiled_files()}
    tools = sorted((REPO_ROOT / "src" / "lemoncrow" / "gateway" / "adapters" / "mcp").glob("tools_*.py"))
    assert tools and not {path.relative_to(REPO_ROOT / "src").as_posix() for path in tools} & real
    assert "lemoncrow/gateway/adapters/mcp/bash.py" not in real


def test_the_compiled_build_compiles_with_the_locked_mypy() -> None:
    """The wheel's mypyc is the mypy uv.lock pins, not whatever PyPI has that day.

    A floating ``mypy>=1.10`` build requirement compiled releases with mypy 2.4.0,
    whose mypyc crashed on code the locked 2.2.0 compiles.
    """
    import tomllib

    requires = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["build-system"]["requires"]
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    locked = {package["name"]: package.get("version") for package in lock["package"]}
    assert f"mypy=={locked['mypy']}" in requires


def test_compiled_build_refuses_git_checkout_and_allows_staging(tmp_path: Path) -> None:
    """A killed mypyc build must never be able to delete tracked checkout source."""
    git_marker = REPO_ROOT / ".git"
    assert git_marker.exists(), "test must run from a Git checkout"

    try:
        hatch_build._assert_isolated_mypyc_build_root(REPO_ROOT)
    except RuntimeError as exc:
        message = str(exc)
        assert "REFUSING in-place compiled build" in message
        assert "scripts/build.sh" in message
    else:
        raise AssertionError("compiled build unexpectedly allowed in Git checkout")

    # A disposable tree without .git is the intended release-build location.
    hatch_build._assert_isolated_mypyc_build_root(tmp_path)
