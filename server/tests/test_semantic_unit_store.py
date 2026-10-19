from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from lemoncrow_server_core.index.analysis import analyze, analyze_foreground
from lemoncrow_server_core.index.contracts import AnalysisStore, SemanticUnit, sha256_hex
from lemoncrow_server_core.index.memory import InMemoryAnalysisStore
from lemoncrow_server_core.index.sqlite import open_sqlite_index

# The stores keep units an engine derived; they never derive them. These tests
# pin that storage contract with opaque units and an opaque version string.
ORG = "org_units"
DATA = b"def alpha():\n    return beta\n\ndef gamma():\n    return delta\n"
DIGEST = sha256_hex(DATA)
PROFILE = "python-3"
SEMANTIC_EMBEDDING_VERSION = "opaque-units/1"
UNITS = (
    SemanticUnit("symbol:0:alpha", 1, 2, "alpha", SEMANTIC_EMBEDDING_VERSION, (0.6, -0.8, 0.0)),
    SemanticUnit("symbol:31:gamma", 4, 5, "gamma", SEMANTIC_EMBEDDING_VERSION, (0.0, 0.0, -0.0)),
)


def test_public_analysis_never_derives_semantic_units() -> None:
    assert analyze(DIGEST, PROFILE, DATA).semantic_units == ()
    assert analyze_foreground(DIGEST, PROFILE, DATA).semantic_units == ()


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[AnalysisStore]:
    if request.param == "memory":
        yield InMemoryAnalysisStore()
        return
    index = open_sqlite_index(tmp_path / "index.sqlite", content_size_cap=1024**2)
    try:
        yield index.backend.analysis
    finally:
        index.close()


def test_derived_units_round_trip_beside_their_artifact(store: AnalysisStore) -> None:
    units = UNITS
    key = (DIGEST, PROFILE)

    # No artifact yet: nothing could ever sweep the row, so none is written.
    store.store_semantic_units(ORG, [(DIGEST, PROFILE, units)], SEMANTIC_EMBEDDING_VERSION)
    assert store.stored_semantic_units(ORG, [key], SEMANTIC_EMBEDDING_VERSION) == {}

    store.ensure(ORG, DIGEST, PROFILE, DATA, artifact=analyze_foreground(DIGEST, PROFILE, DATA))
    store.store_semantic_units(ORG, [(DIGEST, PROFILE, units)], SEMANTIC_EMBEDDING_VERSION)
    assert store.stored_semantic_units(ORG, [key], SEMANTIC_EMBEDDING_VERSION) == {key: units}
    assert store.stored_semantic_units("other_org", [key], SEMANTIC_EMBEDDING_VERSION) == {}
    assert store.stored_semantic_units(ORG, [key], "other-version") == {}
    assert store.stored_semantic_units(ORG, [(DIGEST, "python-1")], SEMANTIC_EMBEDDING_VERSION) == {}

    assert store.sweep(ORG, [DIGEST]) == 1
    assert store.stored_semantic_units(ORG, [key], SEMANTIC_EMBEDDING_VERSION) == {}


def test_a_file_with_no_units_is_remembered_as_such(store: AnalysisStore) -> None:
    store.ensure(ORG, DIGEST, PROFILE, DATA, artifact=analyze_foreground(DIGEST, PROFILE, DATA))
    store.store_semantic_units(ORG, [(DIGEST, PROFILE, ())], SEMANTIC_EMBEDDING_VERSION)
    assert store.stored_semantic_units(ORG, [(DIGEST, PROFILE)], SEMANTIC_EMBEDDING_VERSION) == {(DIGEST, PROFILE): ()}


def test_units_of_another_embedding_version_are_refused(store: AnalysisStore) -> None:
    units = UNITS
    store.ensure(ORG, DIGEST, PROFILE, DATA)
    with pytest.raises(ValueError):
        store.store_semantic_units(ORG, [(DIGEST, PROFILE, units)], "other-version")
