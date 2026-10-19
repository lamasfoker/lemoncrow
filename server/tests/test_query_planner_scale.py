from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import pytest
from lemoncrow_server_core.index.contracts import ManifestEntry, RepoIdentityClaim, sha256_hex
from lemoncrow_server_core.index.manifest import manifest_root
from lemoncrow_server_core.index.query import IndexQuery
from lemoncrow_server_core.index.sqlite import SqliteIndex

CAP = 1024 * 1024


@pytest.mark.skipif(os.environ.get("LEMONCROW_RUN_SCALE_TESTS") != "1", reason="opt-in scalability gate")
def test_50k_narrow_query_plans_materialize_only_bounded_candidates() -> None:
    count = int(os.environ.get("LEMONCROW_PLANNER_SCALE_FILES", "50000"))
    assert count >= 50_000
    with tempfile.TemporaryDirectory() as directory:
        index = SqliteIndex(Path(directory) / "index.sqlite3", content_size_cap=CAP, compact=True)
        try:
            backend = index.backend
            org = "org_i8_scale"
            subject = "alice"
            repo = backend.repos.canonical_repo_id(org, RepoIdentityClaim("", "", "i8-scale"))
            target = b'def AlphaWidget():\n    return "markerword"\n'
            target_digest = sha256_hex(target)
            entries = [
                ManifestEntry(
                    f"src/noise_{number:07d}.py",
                    hashlib.sha256(f"noise-{number}".encode()).hexdigest(),
                    "python-3",
                    0o100644,
                    32,
                )
                for number in range(count - 1)
            ]
            entries.append(ManifestEntry("pkg/target.py", target_digest, "python-3", 0o100644, len(target)))
            manifest = tuple(entries)
            opened = backend.views.open(
                org_id=org,
                repo_id=repo,
                base_revision="HEAD",
                manifest_root=manifest_root(manifest),
                chunk_ids=("all",),
                lease_s=3600,
                content_subject=subject,
                owner_subject=subject,
            )
            backend.views.put_manifest_chunk(
                org_id=org,
                view_id=opened.state.view_id,
                chunk_id="all",
                entries=manifest,
            )
            backend.content.put(org, target_digest, target)
            backend.provenance.record(org, repo, subject, (target_digest,))
            backend.analysis.ensure(org, target_digest, "python-3", target)
            query = IndexQuery(backend, content_size_cap=CAP)

            for text, expected_intent in (
                ("AlphaWidget", "exact_symbol"),
                ("pkg/target.py", "path"),
                ("callers of AlphaWidget", "impact"),
            ):
                assert backend.ops is not None
                before = backend.ops.snapshot().value(
                    "lemoncrow_index_retrieval_candidates_total", channel="code_search"
                )
                answer = query.search(org_id=org, view_id=opened.state.view_id, query=text, limit=8)
                after = backend.ops.snapshot().value(
                    "lemoncrow_index_retrieval_candidates_total", channel="code_search"
                )
                assert answer.plan["intent"] == expected_intent
                assert after - before <= 2
                assert answer.hits
            # Path substring lookup is separately indexed for complete immutable
            # generations. Exercise both a no-match and a late one-hit lookup so
            # this scale gate cannot silently regress to a full manifest scan.
            missing_paths, missing_truncated = backend.views.path_candidates(
                org, opened.state.view_id, "markerword", limit=4_096
            )
            assert missing_paths == {}
            assert missing_truncated is False
            late_paths, late_truncated = backend.views.path_candidates(
                org, opened.state.view_id, f"noise_{count - 2:07d}", limit=4_096
            )
            assert tuple(late_paths) == (f"src/noise_{count - 2:07d}.py",)
            assert late_truncated is False

            # The graph is intentionally cold: the impact request remains useful
            # from its symbol seed and does not reconstruct 50k membership rows.
            impact = query.search(
                org_id=org,
                view_id=opened.state.view_id,
                query="callers of AlphaWidget",
                limit=8,
            )
            assert impact.degraded
        finally:
            index.close()
