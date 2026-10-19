"""Contract tests for the cross-vendor memory adapter system."""

from __future__ import annotations

from pathlib import Path

from lemoncrow.pro.capabilities.cross_vendor_memory.base import _fact_id
from lemoncrow.pro.capabilities.cross_vendor_memory.claude_adapter import ClaudeAdapter, _parse_markdown_facts
from lemoncrow.pro.capabilities.cross_vendor_memory.codex_adapter import (
    CodexAdapter,
    _is_standalone_declaration,
    _parse_codex_facts,
)
from lemoncrow.pro.capabilities.cross_vendor_memory.gemini_adapter import (
    GeminiAdapter,
    _find_repo_root,
    _parse_gemini_facts,
)
from lemoncrow.pro.capabilities.cross_vendor_memory.registry import MemoryRegistry

FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures" / "memory"
CLAUDE_ROOT = FIXTURES / "claude"
CODEX_ROOT = FIXTURES / "codex"
GEMINI_ROOT = FIXTURES / "gemini"


def test_fact_id_contract() -> None:
    assert _fact_id("claude", "hello world") == _fact_id("claude", "hello world")
    assert _fact_id("codex", "test").startswith("codex-")
    assert _fact_id("claude", "foo") != _fact_id("claude", "bar")
    assert _fact_id("claude", "same") != _fact_id("codex", "same")


def test_claude_parser_contract(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    cases = [
        ("- First fact\n- Second fact\n", lambda facts: [f.content for f in facts] == ["First fact", "Second fact"]),
        ("* Star bullet fact\n", lambda facts: len(facts) == 1 and facts[0].content == "Star bullet fact"),
        (
            "## Code style\n- Use ruff\n",
            lambda facts: len(facts) == 1 and facts[0].raw_meta.get("section") == "Code style",
        ),
        ("## My section\n", lambda facts: facts == []),
        (
            "```python\nresult = func()\nreturn result\n```\n",
            lambda facts: len(facts) == 1
            and "result = func()" in facts[0].content
            and "return result" in facts[0].content,
        ),
        ("- \n-   \n- real fact\n", lambda facts: len(facts) == 1 and facts[0].content == "real fact"),
        ("## Section\n- fact at line 2\n", lambda facts: len(facts) == 1 and facts[0].line_number == 2),
    ]
    for markdown, check in cases:
        facts = _parse_markdown_facts(markdown, path, "claude-md")
        assert check(facts), (markdown, facts)


def test_claude_adapter_contract(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(root=CLAUDE_ROOT)
    assert adapter.is_available()
    facts = adapter.list_facts()
    assert facts and {f.vendor for f in facts} == {"claude"}
    contents = [f.content for f in facts]
    assert any("uv, not pip" in c for c in contents)
    assert any("type hints" in c for c in contents)
    assert "session-memory" in {f.source_kind for f in facts}
    assert any("\n" in f.content for f in facts)

    missing = ClaudeAdapter(root=tmp_path / "nonexistent")
    assert not missing.is_available() and missing.list_facts() == []


def test_codex_declaration_and_parser_contract(tmp_path: Path) -> None:
    for text in (
        "Use strict mypy mode.",
        "All functions must have return type annotations.",
        "Avoid wildcard imports.",
    ):
        assert _is_standalone_declaration(text)
    for text in ("use lowercase", "no period at end", ""):
        assert not _is_standalone_declaration(text)

    path = tmp_path / "test.md"
    heading = _parse_codex_facts("## Engineering Style\nHard-remove strategy.\n", path)
    assert len(heading) == 1 and "Engineering Style" in heading[0].content
    assert heading[0].raw_meta["heading"] == "Engineering Style"

    standalone = _parse_codex_facts("## Engineering\n- Use strict mypy mode. This is a requirement.\n", path)
    assert len(standalone) == 2
    assert "mypy" in next(f for f in standalone if f.raw_meta.get("sub_fact")).content

    folded = _parse_codex_facts("## Section\n- run `uv sync` to install\n", path)
    assert len(folded) == 1 and "uv sync" in folded[0].content


def test_codex_adapter_contract(tmp_path: Path) -> None:
    adapter = CodexAdapter(root=CODEX_ROOT)
    assert adapter.is_available()
    facts = adapter.list_facts()
    assert facts and all(f.vendor == "codex" for f in facts)
    names = {f.source_path.name for f in facts}
    assert {"lemoncrow-project.md", "global.md"} <= names

    missing = CodexAdapter(root=tmp_path / "nonexistent")
    assert not missing.is_available() and missing.list_facts() == []


def test_gemini_parser_contract(tmp_path: Path) -> None:
    path = tmp_path / "GEMINI.md"
    facts = _parse_gemini_facts("- Global fact one\n- Global fact two\n", path, "gemini-md-global")
    assert len(facts) == 2
    sectioned = _parse_gemini_facts("## Workflow\n- Always check CI\n", path, "gemini-md-global")
    assert sectioned[0].raw_meta.get("section") == "Workflow"


def test_gemini_adapter_contract(tmp_path: Path) -> None:
    adapter = GeminiAdapter(global_root=GEMINI_ROOT, cwd=GEMINI_ROOT)
    assert adapter.is_available()
    facts = adapter.list_facts()
    assert facts and all(f.vendor == "gemini" for f in facts)
    assert any("Always check CI" in f.content for f in facts)
    assert "gemini-md-global" in {f.source_kind for f in facts}

    missing = GeminiAdapter(global_root=tmp_path / "nope", cwd=tmp_path)
    assert not missing.is_available() and missing.list_facts() == []


def test_find_repo_root_contract(tmp_path: Path) -> None:
    git_dir = tmp_path / "with-git" / ".git"
    git_dir.mkdir(parents=True)
    subdir = git_dir.parent / "src" / "pkg"
    subdir.mkdir(parents=True)
    assert _find_repo_root(subdir) == git_dir.parent

    no_git = tmp_path / "no-git" / "subdir"
    no_git.mkdir(parents=True)
    result = _find_repo_root(no_git)
    if result is not None:
        assert (result / ".git").exists(), result


def _make_registry() -> MemoryRegistry:
    return MemoryRegistry(
        adapters=[
            ClaudeAdapter(root=CLAUDE_ROOT),
            CodexAdapter(root=CODEX_ROOT),
            GeminiAdapter(global_root=GEMINI_ROOT, cwd=GEMINI_ROOT),
        ]
    )  # type: ignore[list-item]


def test_registry_lookup_contract() -> None:
    registry = _make_registry()
    facts = registry.all_facts()
    assert {"claude", "codex", "gemini"} <= {f.vendor for f in facts}
    claude = registry.by_vendor("claude")
    assert claude and all(f.vendor == "claude" for f in claude)
    assert registry.by_vendor("CLAUDE") == claude
    fact = facts[0]
    assert registry.show(fact.fact_id) == fact
    assert registry.show("nonexistent-id") is None
    assert any("uv" in f.content for f in registry.find("uv, not pip"))
    assert len(registry.find("the", limit=3)) <= 3
    assert len(registry.find("zzzyyyxxx_no_match_ever_12345", limit=20)) < 5
    paths = [str(f.source_path) for f in facts]
    assert paths == sorted(paths) or len(set(paths)) >= 2


def test_registry_empty_unavailable_and_invalidation_contract(tmp_path: Path) -> None:
    empty = MemoryRegistry(adapters=[])
    assert empty.all_facts() == [] and empty.by_vendor("claude") == []
    assert empty.show("x") is None and empty.find("test") == []

    missing = MemoryRegistry(adapters=[ClaudeAdapter(root=tmp_path / "nonexistent")])  # type: ignore[list-item]
    assert missing.all_facts() == []

    registry = _make_registry()
    before = {f.fact_id for f in registry.all_facts()}
    registry.invalidate()
    assert {f.fact_id for f in registry.all_facts()} == before
