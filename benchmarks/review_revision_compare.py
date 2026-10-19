"""Reproducible local benchmark for Review revision history/comparison.

This intentionally benchmarks the persisted Review-local API path, not generic
Git diff or mutable worktree capture. It records medians/p95 for:
- revision history load;
- first ("cold") rev A -> B comparison in this process;
- repeated warm comparison of the same pair;
- repeated switching among several immutable bases;
- historical annotation/context projection.

No threshold is enforced. The output is a baseline artifact for regression
investigation, not a brittle CI performance gate.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pygit2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, refresh, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "review-benchmark-token"
PORT = 45998
BASE = f"http://127.0.0.1:{PORT}"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _init_repo(root: Path, files: int) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Review Benchmark"
    repo.config["user.email"] = "benchmark@example.invalid"
    for index in range(files):
        _write(
            root,
            f"src/file_{index:03d}.py",
            f"def value_{index}() -> int:\n    return {index}\n",
        )
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Review Benchmark", "benchmark@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", sig, sig, "benchmark base", tree, [])
    return repo


def _build(repo_root: Path, store_root: Path):
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    return rng, build


def _elapsed_ms(call: Callable[[], Any]) -> tuple[float, Any]:
    started = time.perf_counter()
    value = call()
    return (time.perf_counter() - started) * 1000.0, value


def _stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    if not ordered:
        return {"median_ms": 0.0, "p95_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0}
    p95_index = min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))
    return {
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


def _request(client: TestClient, path: str):
    response = client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    response.raise_for_status()
    return response.json()


def run(*, revisions: int, files: int, iterations: int) -> dict[str, Any]:
    if revisions < 2:
        raise ValueError("--revisions must be >= 2")
    if files < 2:
        raise ValueError("--files must be >= 2")
    if iterations < 3:
        raise ValueError("--iterations must be >= 3")

    with tempfile.TemporaryDirectory(prefix="lc-review-compare-bench-") as tmp:
        root = Path(tmp)
        repo_root = root / "repo"
        store_root = root / "store"
        _init_repo(repo_root, files)

        # rev 0
        _write(repo_root, "src/file_000.py", "def value_0() -> int:\n    return 1000\n")
        store = ReviewStore(store_root)
        rng, build = _build(repo_root, store_root)
        session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
        snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)

        app = FastAPI()
        register_review_api(
            app,
            store,
            auth_dependency=make_token_dependency(TOKEN),
            repo_root=repo_root,
            port=PORT,
        )
        client = TestClient(app, base_url=BASE)
        review = session.id
        headers = {"Authorization": f"Bearer {TOKEN}"}

        # Seed both human cursors at rev 0. They must remain durable while later
        # source checkpoints are added.
        marked = client.post(
            f"/api/reviews/{review}/marks",
            headers=headers,
            json={"unit_key": "src/file_000.py", "state": "reviewed"},
        )
        marked.raise_for_status()
        comment = client.post(
            f"/api/reviews/{review}/annotations",
            headers=headers,
            json={
                "path": "src/file_000.py",
                "start_line": 2,
                "body": "benchmark feedback on rev 0",
                "kind": "comment",
            },
        )
        comment.raise_for_status()

        # Build an immutable revision chain. Each new checkpoint keeps earlier
        # working-tree edits and changes one additional file, creating an
        # increasingly substantial persisted comparison without committing.
        for number in range(1, revisions):
            file_index = number % files
            _write(
                repo_root,
                f"src/file_{file_index:03d}.py",
                (f"def value_{file_index}() -> int:\n" f"    return {1000 + number}\n" f"\nREVISION = {number}\n"),
            )
            rng, build = _build(repo_root, store_root)
            result = refresh(
                store,
                session,
                repo_root,
                store_root=store_root,
                rng=rng,
                build=build,
            )
            if not result.created:
                raise RuntimeError(f"benchmark failed to create rev {number}")

        latest = revisions - 1
        midpoint = latest // 2

        cold_ms, cold_payload = _elapsed_ms(
            lambda: _request(client, f"/api/reviews/{review}/compare?from_revision=0&to_revision={latest}")
        )

        warm_samples: list[float] = []
        for _ in range(iterations):
            elapsed, _ = _elapsed_ms(
                lambda: _request(client, f"/api/reviews/{review}/compare?from_revision=0&to_revision={latest}")
            )
            warm_samples.append(elapsed)

        switch_bases = sorted({0, midpoint, max(0, latest - 1)})
        switch_samples: list[float] = []
        for index in range(iterations):
            base = switch_bases[index % len(switch_bases)]
            elapsed, _ = _elapsed_ms(
                lambda base=base: _request(
                    client,
                    f"/api/reviews/{review}/compare?from_revision={base}&to_revision={latest}",
                )
            )
            switch_samples.append(elapsed)

        history_samples: list[float] = []
        annotation_samples: list[float] = []
        for _ in range(iterations):
            elapsed, _ = _elapsed_ms(lambda: _request(client, f"/api/reviews/{review}/revisions"))
            history_samples.append(elapsed)
            elapsed, _ = _elapsed_ms(
                lambda: _request(client, f"/api/reviews/{review}/revisions/{midpoint}/annotations")
            )
            annotation_samples.append(elapsed)

        default_payload = _request(client, f"/api/reviews/{review}/compare")
        history_payload = _request(client, f"/api/reviews/{review}/revisions")

        return {
            "shape": {
                "revisions": revisions,
                "files": files,
                "iterations": iterations,
                "latest_revision_number": latest,
                "cold_compare_files": cold_payload["code_summary"]["files"],
            },
            "semantics": {
                "default_base_revision_number": default_payload["from_revision"]["revision_number"],
                "default_target_revision_number": default_payload["to_revision"]["revision_number"],
                "last_reviewed_revision_number": history_payload["last_reviewed_revision_number"],
                "last_commented_revision_number": history_payload["last_commented_revision_number"],
            },
            "timings": {
                "compare_cold": {"elapsed_ms": round(cold_ms, 3)},
                "compare_warm": _stats(warm_samples),
                "compare_switch": _stats(switch_samples),
                "history": _stats(history_samples),
                "historical_annotations": _stats(annotation_samples),
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revisions", type=int, default=12)
    parser.add_argument("--files", type=int, default=24)
    parser.add_argument("--iterations", type=int, default=25)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    result = run(revisions=args.revisions, files=args.files, iterations=args.iterations)
    encoded = json.dumps(result, indent=2, sort_keys=True)
    print(encoded)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
