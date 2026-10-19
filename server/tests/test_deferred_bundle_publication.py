from __future__ import annotations

import time
from dataclasses import replace

from lemoncrow_server_core.index.contracts import ManifestEntry, RepoIdentityClaim, sha256_hex
from lemoncrow_server_core.index.deferred import DeferredIndexWorker, MemoryDeferredJobStore, enqueue_deferred_layer
from lemoncrow_server_core.index.manifest import chunk_manifest, manifest_root
from lemoncrow_server_core.index.memory import build_memory_backend

ORG = "org_publish"
CAP = 1024 * 1024


def test_generation_link_worker_publishes_after_ready_build() -> None:
    base = build_memory_backend(content_size_cap=CAP, inline_rebuild_budget=1)
    calls: list[tuple[str, str]] = []
    backend = replace(
        base,
        deferred_jobs=MemoryDeferredJobStore(),
        publish_generation=lambda org_id, view_id: not calls.append((org_id, view_id)),
    )
    repo = backend.repos.canonical_repo_id(ORG, RepoIdentityClaim("", "", "publish-repo"))
    files = {
        "a.py": b"from b import beta\n\ndef alpha():\n    return beta()\n",
        "b.py": b"def beta():\n    return 2\n",
    }
    entries = tuple(
        ManifestEntry(path, sha256_hex(data), "python-2", 0o100644, len(data)) for path, data in sorted(files.items())
    )
    chunks = chunk_manifest(entries)
    opened = backend.views.open(
        org_id=ORG,
        repo_id=repo,
        base_revision="a" * 40,
        manifest_root=manifest_root(entries),
        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
        lease_s=3600,
        content_subject="alice",
        owner_subject="alice",
    )
    for chunk in chunks:
        backend.views.put_manifest_chunk(
            org_id=ORG,
            view_id=opened.state.view_id,
            chunk_id=chunk.chunk_id,
            entries=chunk.entries,
        )
    for path, data in files.items():
        entry = next(item for item in entries if item.path == path)
        backend.content.put(ORG, entry.content_digest, data)
        backend.provenance.record(ORG, repo, "alice", (entry.content_digest,))

    job = enqueue_deferred_layer(backend, ORG, opened.state.view_id, "link")
    assert job is not None and job.target_kind == "generation"
    worker = DeferredIndexWorker(backend, max_files=100, retry_delay_s=0.01)
    try:
        worker.start()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            current = backend.deferred_jobs.get(job.job_id)
            if current is not None and current.state == "completed":
                break
            time.sleep(0.01)
        current = backend.deferred_jobs.get(job.job_id)
        assert current is not None and current.state == "completed"
        assert calls == [(ORG, opened.state.view_id)]
    finally:
        worker.close()
