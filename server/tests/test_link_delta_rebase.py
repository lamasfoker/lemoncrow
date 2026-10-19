"""A dirty View's link graph is a delta over its generation graph, which grows
as more content becomes addressable: the delta must follow it, or the View
gains edges for the files it deleted while it still reports READY."""

from __future__ import annotations

from pathlib import Path

from lemoncrow_server_core.index.contracts import ManifestEntry, RepoIdentityClaim, sha256_hex
from lemoncrow_server_core.index.manifest import chunk_manifest, manifest_root
from lemoncrow_server_core.index.repositories import RepositoryIndex

CAP = 1024**2
ORG = "org_generation"

FILES = {
    "a.py": b"from b import beta\n\ndef alpha():\n    return beta()\n",
    "b.py": b"def beta():\n    return 1\n",
    "c.py": b"def charlie():\n    return 3\n",
}


def _entries(files: dict[str, bytes]) -> tuple[ManifestEntry, ...]:
    return tuple(
        ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data)) for path, data in sorted(files.items())
    )


def _open(owner: RepositoryIndex, files: dict[str, bytes], *, held: set[str]) -> tuple[str, str]:
    repository = owner.backend.repos.canonical_repo_id(ORG, RepoIdentityClaim("", "", "drift-repo"))
    entries = _entries(files)
    chunks = chunk_manifest(entries)
    opened = owner.backend.views.open(
        org_id=ORG,
        repo_id=repository,
        base_revision="a" * 40,
        manifest_root=manifest_root(entries),
        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
        lease_s=100_000,
        content_subject="alice",
        owner_subject="alice",
    )
    view_id = opened.state.view_id
    for chunk_id in opened.state.pending_chunks:
        chunk = next(item for item in chunks if item.chunk_id == chunk_id)
        owner.backend.views.put_manifest_chunk(
            org_id=ORG, view_id=view_id, chunk_id=chunk.chunk_id, entries=chunk.entries
        )
    leaf = owner.backend.for_view(ORG, view_id)
    for path, data in files.items():
        if path not in held:
            continue
        digest = sha256_hex(data)
        leaf.content.put(ORG, digest, data)
        leaf.analysis.ensure(ORG, digest, "python-1", data)
        leaf.provenance.record(ORG, repository, "alice", (digest,))
    return repository, view_id


def test_delta_over_a_generation_graph_that_grows(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        # alice has not (yet) handed the server b.py's base bytes.
        _repo, dirty = _open(owner, FILES, held={"a.py", "c.py"})
        leaf = owner.backend.for_view(ORG, dirty)
        # The dirty View deletes b.py, so it never needs b.py's base bytes.
        state = leaf.views.apply_overlay(
            org_id=ORG, view_id=dirty, expected_view_revision=0, client_seq=1, entries=(), removed_paths=("b.py",)
        )
        leaf.links.note_revision(ORG, dirty, state.view_revision)
        status = leaf.links.refresh(ORG, dirty, budget=100, wait=True)
        assert status.ready, status
        before = leaf.links.edges(ORG, dirty)
        assert not any("b.py" in (edge.src_path, edge.dst_path) for edge in before), before

        # Later, a clean View of the same subject on the same generation uploads
        # b.py's base bytes and refreshes: the generation graph grows.
        _repo, clean = _open(owner, FILES, held={"a.py", "b.py", "c.py"})
        assert leaf.links.refresh(ORG, clean, budget=100, wait=True).ready
        assert any(edge.dst_path == "b.py" for edge in leaf.links.edges(ORG, clean))

        # The dirty View did not change; it is still READY at its revision.
        assert leaf.links.status(ORG, dirty).ready
        after = leaf.links.edges(ORG, dirty)
        stale = [edge for edge in after if "b.py" in (edge.src_path, edge.dst_path)]
        assert not stale, f"dirty View that deleted b.py now reports edges into b.py: {stale}"
    finally:
        owner.close()


def test_deleted_file_edges_stay_gone_after_the_next_edit(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, dirty = _open(owner, FILES, held={"a.py", "c.py"})
        leaf = owner.backend.for_view(ORG, dirty)
        state = leaf.views.apply_overlay(
            org_id=ORG, view_id=dirty, expected_view_revision=0, client_seq=1, entries=(), removed_paths=("b.py",)
        )
        leaf.links.note_revision(ORG, dirty, state.view_revision)
        assert leaf.links.refresh(ORG, dirty, budget=100, wait=True).ready
        _repo, clean = _open(owner, FILES, held={"a.py", "b.py", "c.py"})
        assert leaf.links.refresh(ORG, clean, budget=100, wait=True).ready

        # The dirty View edits an unrelated file and refreshes incrementally.
        data = b"def charlie():\n    return 33\n"
        entry = ManifestEntry("c.py", sha256_hex(data), "python-1", 0o100644, len(data))
        leaf.content.put(ORG, entry.content_digest, data)
        leaf.analysis.ensure(ORG, entry.content_digest, "python-1", data)
        leaf.provenance.record(ORG, repo, "alice", (entry.content_digest,))
        state = leaf.views.apply_overlay(
            org_id=ORG, view_id=dirty, expected_view_revision=1, client_seq=2, entries=(entry,), removed_paths=()
        )
        leaf.links.note_revision(ORG, dirty, state.view_revision)
        assert leaf.links.refresh(ORG, dirty, budget=100, wait=True).ready
        stale = [edge for edge in leaf.links.edges(ORG, dirty) if "b.py" in (edge.src_path, edge.dst_path)]
        assert not stale, f"after a later edit the deleted b.py still has edges: {stale}"
    finally:
        owner.close()
