from __future__ import annotations

from pathlib import Path

import pytest
from lemoncrow_server_core.index.analysis import analyze
from lemoncrow_server_core.index.contracts import sha256_hex
from lemoncrow_server_core.index.memory import InMemoryAnalysisStore
from lemoncrow_server_core.index.sqlite import SqliteIndex

ORG = "org_restore"
CAP = 1024 * 1024
DATA = b"def alpha():\n    return beta()\n\ndef beta():\n    return 2\n"


@pytest.mark.parametrize("kind", ("memory", "sqlite", "compact"))
def test_restore_installs_searchable_artifact_without_derivation(kind: str, tmp_path: Path) -> None:
    digest = sha256_hex(DATA)
    artifact = analyze(digest, "python-2", DATA)
    owner = None
    if kind == "memory":
        store = InMemoryAnalysisStore()
    else:
        owner = SqliteIndex(
            tmp_path / f"{kind}.sqlite3",
            content_size_cap=CAP,
            compact=kind == "compact",
        )
        store = owner.backend.analysis
    try:
        restored = store.restore(ORG, artifact)
        assert restored == artifact
        assert store.get(ORG, digest, "python-2") == artifact
        assert store.derivations == 0
        candidates, truncated = store.lexical_candidates(ORG, "alpha", limit=10)
        assert candidates == (digest,)
        assert truncated is False
        assert store.definitions_named(ORG, (digest,), "alpha")[digest][0].name == "alpha"
        assert store.nearest(ORG, (digest,), artifact.embedding, 1)[0][0] == digest
        # Idempotent bundle replay remains a restore, not a derivation.
        assert store.restore(ORG, artifact) == artifact
        assert store.derivations == 0
    finally:
        if owner is not None:
            owner.close()
