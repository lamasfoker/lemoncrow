"""Unit tests for the honest code-intel avoided-read credit (pure functions).

These exercise the counterfactual directly: a snippet-bearing reference that is
never read earns exactly one deferred credit; a snippet-less reference, or one
that is later read, earns nothing.
"""

from __future__ import annotations

from lemoncrow.core.capabilities import code_intel_credit as cic


def test_credit_extraction_contract() -> None:
    """case: test_node_with_inline_source_is_credited"""
    result = {"name": "helper", "path": "src/h.py", "source": "def helper(): ..."}
    assert cic.extract_credited_paths("node", result) == ["src/h.py"]
    "case: test_node_without_inline_source_earns_nothing"
    assert cic.extract_credited_paths("node", {"name": "helper", "path": "src/h.py"}) == []
    assert cic.extract_credited_paths("node", {"name": "helper", "path": "src/h.py", "source": "   "}) == []
    "case: test_explore_inline_file_source_is_credited"
    result = {
        "files": [
            {"file_path": "src/main.py", "source_sections": [{"content": "1\tdef main(): ..."}]},
            {"file_path": "src/empty.py", "source_sections": [{"content": ""}]},
        ]
    }
    assert cic.extract_credited_paths("explore", result) == ["src/main.py"]
    "case: test_explore_relationship_snippets_are_not_credited"
    result = {
        "files": [{"file_path": "src/main.py", "source_sections": [{"content": "def main(): ..."}]}],
        "relationships": {
            "callers": [{"symbol_name": "main", "related": [{"path": "src/caller.py", "snippet": "main()"}]}],
            "usages": [{"symbol_name": "main", "references": [{"path": "src/use.py", "snippet": "main()"}]}],
        },
    }
    paths = cic.extract_credited_paths("explore", result)
    assert paths == ["src/main.py"]
    assert "src/caller.py" not in paths
    assert "src/use.py" not in paths
    "case: test_partial_snippet_tools_are_not_credited"
    related = {"related": [{"name": "run", "path": "src/a.py", "snippet": "def run(): ..."}]}
    refs_flat = {"references": [{"path": "src/x.py", "line": 10, "snippet": "foo()"}]}
    refs_grouped = {"references": {"src/c.py": [{"path": "src/c.py", "snippet": "charge()"}]}}
    assert cic.extract_credited_paths("callers", related) == []
    assert cic.extract_credited_paths("callees", related) == []
    assert cic.extract_credited_paths("usages", refs_flat) == []
    assert cic.extract_credited_paths("usages", refs_grouped) == []
    "case: test_non_credit_eligible_tools_return_empty"
    snippet_ref = {"related": [{"path": "src/a.py", "snippet": "x"}]}
    for tool in ("read", "search", "grep", "callers", "usages"):
        assert cic.extract_credited_paths(tool, snippet_ref) == []
    "case: test_non_dict_result_returns_empty"
    assert cic.extract_credited_paths("node", None) == []
    assert cic.extract_credited_paths("explore", "oops") == []
    assert cic.extract_credited_paths("node", []) == []
    "case: test_extract_distinct_per_file"
    result = {
        "files": [
            {"file_path": "src/a.py", "source_sections": [{"content": "one"}, {"content": "two"}]},
            {"file_path": "src/b.py", "source_sections": [{"content": "three"}]},
        ]
    }
    assert cic.extract_credited_paths("explore", result) == ["src/a.py", "src/b.py"]


def test_credit_lifecycle_contract() -> None:
    """case: test_record_then_read_then_no_credit"""
    state: dict = {}
    state = cic.record_pending(state, "callers", ["src/a.py"])
    assert len(state["code_intel_pending"]) == 1
    state = cic.consume_reads(state, ["src/a.py"])
    assert state["code_intel_pending"] == []
    for _ in range(10):
        state, credits = cic.tick_and_credit(state, threshold=8)
        assert credits == []
    "case: test_record_never_read_credits_exactly_once"
    state: dict = {}
    state = cic.record_pending(state, "usages", ["src/x.py"])
    for _ in range(7):
        state, credits = cic.tick_and_credit(state, threshold=8)
        assert credits == []
    state, credits = cic.tick_and_credit(state, threshold=8)
    assert credits == [{"tool": "usages", "path": "src/x.py"}]
    assert state["code_intel_pending"] == []
    state, credits = cic.tick_and_credit(state, threshold=8)
    assert credits == []
    "case: test_duplicate_surfacing_single_pending_entry"
    state: dict = {}
    state = cic.record_pending(state, "callers", ["src/a.py"])
    state = cic.record_pending(state, "usages", ["src/a.py"])
    assert len(state["code_intel_pending"]) == 1
    assert state["code_intel_pending"][0]["tool"] == "callers"
    "case: test_reset_pending_clears"
    state: dict = {}
    state = cic.record_pending(state, "node", ["src/a.py", "src/b.py"])
    assert len(state["code_intel_pending"]) == 2
    state = cic.reset_pending(state)
    assert state["code_intel_pending"] == []


def test_credit_defensive_contract() -> None:
    """case: test_functions_tolerate_garbage_state"""
    assert cic.record_pending(None, "callers", ["x"]) is None
    assert cic.consume_reads(None, ["x"]) is None
    bad, credits = cic.tick_and_credit(None, threshold=8)
    assert bad is None and credits == []
    state = {"code_intel_pending": "not-a-list"}
    state, credits = cic.tick_and_credit(state, threshold=8)
    assert credits == []
    "case: test_tick_with_bad_threshold_is_noop"
    state: dict = {}
    state = cic.record_pending(state, "callers", ["src/a.py"])
    state, credits = cic.tick_and_credit(state, threshold="oops")
    assert credits == []
    assert len(state["code_intel_pending"]) == 1
