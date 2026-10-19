from __future__ import annotations

from pathlib import Path

from lemoncrow_client.lite.semantic import (
    LiteSemanticClient,
    SemanticMatch,
    fuse_ranked_paths,
    is_semantic_intent_query,
    semantic_enabled,
)


def test_semantic_mode_is_explicit_opt_in() -> None:
    assert not semantic_enabled({})
    assert not semantic_enabled({"LEMONCROW_LITE_SEMANTIC": "0"})
    assert semantic_enabled({"LEMONCROW_LITE_SEMANTIC": "1"})
    assert semantic_enabled({"LEMONCROW_LITE_SEMANTIC": "true"})


def test_semantic_intent_uses_general_prose_shape() -> None:
    assert is_semantic_intent_query("handle missing template errors")
    assert is_semantic_intent_query("permission lookup dictionary")
    assert not is_semantic_intent_query("Widget::render value state")
    assert not is_semantic_intent_query("foo -> bar baz")


def test_semantic_fusion_preserves_lexical_rank_one() -> None:
    fused = fuse_ranked_paths(
        ["lex-first.py", "shared.py", "lex-tail.py"],
        ["semantic-first.py", "shared.py", "lex-first.py"],
    )
    assert fused[0] == "lex-first.py"
    assert set(fused) == {
        "lex-first.py",
        "shared.py",
        "lex-tail.py",
        "semantic-first.py",
    }
    assert fused.index("shared.py") < fused.index("lex-tail.py")


def test_semantic_fusion_without_lexical_results_keeps_semantic_order() -> None:
    assert fuse_ranked_paths([], ["b.py", "a.py"]) == ["b.py", "a.py"]


def test_semantic_search_stays_empty_until_cache_is_ready(tmp_path: Path) -> None:
    client = LiteSemanticClient({"LEMONCROW_LITE_SEMANTIC": "1"})
    assert client.search(tmp_path, "describe behavior here") == []


def test_semantic_search_parses_helper_matches(tmp_path: Path, monkeypatch) -> None:
    client = LiteSemanticClient({"LEMONCROW_LITE_SEMANTIC": "1"})
    root = tmp_path.resolve()
    client._ready.add(root)

    def fake_request(_payload):
        return {
            "matches": [
                {"path": "src/a.py", "line": 7, "end_line": 12},
                {"path": "src/a.py", "line": 30, "end_line": 35},
                {"path": "src/b.py", "line": 1, "end_line": 2},
            ]
        }

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.search(root, "describe behavior here") == [
        SemanticMatch(path="src/a.py", line=7, end_line=12),
        SemanticMatch(path="src/b.py", line=1, end_line=2),
    ]


def test_lite_mcp_fuses_semantic_tail_without_displacing_lexical_rank_one(tmp_path: Path) -> None:
    import subprocess

    from lemoncrow_client.lite.mcp import LiteMcp

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "lexical.py").write_text(
        "def permission_lookup_dictionary():\n    return {'permission': 'lookup'}\n",
        encoding="utf-8",
    )
    (root / "semantic.py").write_text(
        "def unrelated_name():\n    return 'authorization registry'\n",
        encoding="utf-8",
    )

    class FakeSemantic:
        active = True

        @staticmethod
        def ready(_root: Path) -> bool:
            return True

        @staticmethod
        def search(
            _root: Path,
            _query: str,
            *,
            paths=None,
            limit: int = 100,
        ) -> list[SemanticMatch]:
            del paths, limit
            return [SemanticMatch(path="semantic.py", line=1, end_line=2)]

        @staticmethod
        def close() -> None:
            return None

    mcp = LiteMcp(
        environment={
            "LEMONCROW_WORKSPACE_ROOT": str(root),
            "LEMONCROW_HOME": str(tmp_path / "state"),
            "LEMONCROW_LITE_SEMANTIC": "1",
            "HOME": str(tmp_path),
        }
    )
    try:
        mcp.prepare()
        mcp._semantic.close()
        mcp._semantic = FakeSemantic()  # type: ignore[assignment]
        result = mcp.call("code_search", {"query": "permission lookup dictionary", "limit": 2})
        payload = result["structuredContent"]
        files = payload["files"]
        assert files[0]["path"] == "lexical.py"
        semantic = next(item for item in files if item["path"] == "semantic.py")
        assert "semantic" in semantic["channels"]
        assert payload["semantic"]["provider"] == "potion-code-16M-v2-int8"
    finally:
        mcp.close()


def test_semantic_adopts_matching_warm_cache(tmp_path: Path, monkeypatch) -> None:
    client = LiteSemanticClient({"LEMONCROW_LITE_SEMANTIC": "1"})
    root = tmp_path.resolve()
    seen = []

    def fake_request(payload):
        seen.append(dict(payload))
        return {"ready": True, "generation": 4, "rows": 12}

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.adopt_prepared(root)
    assert client.ready(root)
    assert seen == [{"op": "status", "repo_root": str(root)}]


def test_semantic_search_attempts_warm_adoption_before_falling_back(tmp_path: Path, monkeypatch) -> None:
    client = LiteSemanticClient({"LEMONCROW_LITE_SEMANTIC": "1"})
    root = tmp_path.resolve()

    monkeypatch.setattr(client, "adopt_prepared", lambda _root: False)
    monkeypatch.setattr(client, "_request", lambda _payload: (_ for _ in ()).throw(AssertionError("no search")))

    assert client.search(root, "describe behavior here") == []


def test_semantic_adopt_prepared_marks_warm_cache_ready(tmp_path: Path, monkeypatch) -> None:
    client = LiteSemanticClient({"LEMONCROW_LITE_SEMANTIC": "1"})
    root = tmp_path.resolve()
    monkeypatch.setattr(client, "_request", lambda payload: {"ready": payload["op"] == "status"})

    assert client.adopt_prepared(root)
    assert client.ready(root)


def test_semantic_search_does_not_prepare_implicitly(tmp_path: Path, monkeypatch) -> None:
    client = LiteSemanticClient({"LEMONCROW_LITE_SEMANTIC": "1"})
    calls: list[object] = []
    monkeypatch.setattr(client, "_request", lambda payload: calls.append(payload) or {})

    assert client.search(tmp_path, "describe missing template behavior") == []
    assert [call["op"] for call in calls] == ["status"]


def test_semantic_warm_requires_adopted_cache(tmp_path: Path, monkeypatch) -> None:
    client = LiteSemanticClient({"LEMONCROW_LITE_SEMANTIC": "1"})
    calls: list[object] = []
    monkeypatch.setattr(client, "_request", lambda payload: calls.append(payload) or {"ready": True})

    assert not client.warm(tmp_path)
    assert calls == []


def test_semantic_warm_uses_helper_after_adoption(tmp_path: Path, monkeypatch) -> None:
    client = LiteSemanticClient({"LEMONCROW_LITE_SEMANTIC": "1"})
    root = tmp_path.resolve()
    client._ready.add(root)
    calls: list[object] = []
    monkeypatch.setattr(client, "_request", lambda payload: calls.append(payload) or {"ready": True})

    assert client.warm(root)
    assert [call["op"] for call in calls] == ["warm"]
