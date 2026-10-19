from __future__ import annotations

from lemoncrow.infra.runtime.lite_semantic_helper import (
    _DIMENSIONS,
    _SCALE,
    _SCHEMA_VERSION,
    _chunks,
    _CompactSemanticIndex,
    _line_boundaries,
)


def test_line_chunking_is_bounded_and_complete() -> None:
    raw = (("x" * 300 + "\n") * 8).encode()
    boundaries = _line_boundaries(raw, 750)
    assert boundaries[0].start == 0
    assert boundaries[-1].end == len(raw)
    assert all(boundary.end > boundary.start for boundary in boundaries)
    assert len(boundaries) > 1


def test_unknown_parser_falls_back_to_line_chunks() -> None:
    source = "alpha = 1\n" + ("beta = alpha + 1\n" * 80)

    def missing_parser(_language: str):
        raise RuntimeError("no parser")

    chunks = _chunks(source, "example.py", missing_parser)
    assert chunks
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line >= 80
    assert "".join(chunk.content for chunk in chunks) == source


def test_path_scope_is_directory_aware() -> None:
    assert _CompactSemanticIndex._allowed("src/pkg/a.py", ["src"])
    assert _CompactSemanticIndex._allowed("src/pkg/a.py", ["src/pkg"])
    assert not _CompactSemanticIndex._allowed("tests/a.py", ["src"])


def test_status_rejects_cache_after_lexical_generation_advances(tmp_path) -> None:
    import sqlite3
    from pathlib import Path

    root = Path(tmp_path)
    (root / ".git").mkdir()
    lite_dir = root / ".lemoncrow" / "workspace"
    lite_dir.mkdir(parents=True)
    lite_db = lite_dir / "runtime-lite-v3.sqlite3"
    connection = sqlite3.connect(lite_db)
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta(key,value) VALUES('generation','7')")
    connection.execute("CREATE TABLE files(path TEXT PRIMARY KEY, identity TEXT, text_indexed INTEGER)")
    connection.commit()
    connection.close()

    index = _CompactSemanticIndex()
    state = index._state(root)
    index._set_meta(state, "schema_version", _SCHEMA_VERSION)
    index._set_meta(state, "active_generation", 2)
    index._set_meta(state, "lite_generation", 7)
    index._set_meta(state, "rows", 1)
    index._set_meta(state, "dimensions", _DIMENSIONS)
    index._set_meta(state, "scale", _SCALE)
    state.commit()
    state.close()

    vectors, starts, ends = index._generation_paths(root, 2)
    vectors.write_bytes(bytes(_DIMENSIONS))
    starts.write_bytes((1).to_bytes(4, "little", signed=True))
    ends.write_bytes((2).to_bytes(4, "little", signed=True))

    assert index.status(root)["ready"] is True

    connection = sqlite3.connect(lite_db)
    connection.execute("UPDATE meta SET value='8' WHERE key='generation'")
    connection.commit()
    connection.close()

    assert index.status(root) == {"ready": False}


def test_status_is_false_without_a_complete_generation(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    runtime = _CompactSemanticIndex()
    assert runtime.status(root) == {"ready": False}


def test_warm_rejects_missing_generation_without_loading_model(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    runtime = _CompactSemanticIndex()
    monkeypatch.setattr(
        runtime,
        "_model_instance",
        lambda: (_ for _ in ()).throw(AssertionError("model should not load")),
    )

    assert runtime.warm(root) == {"ready": False}


def test_scope_name_uses_nearest_preceding_structural_symbol() -> None:
    scopes = ([3, 10, 25], ["Outer", "render", "helper"])

    assert _CompactSemanticIndex._scope_name(scopes, 1) == ""
    assert _CompactSemanticIndex._scope_name(scopes, 3) == "Outer"
    assert _CompactSemanticIndex._scope_name(scopes, 18) == "render"
    assert _CompactSemanticIndex._scope_name(scopes, 40) == "helper"
    assert _CompactSemanticIndex._scope_name(None, 40) == ""
