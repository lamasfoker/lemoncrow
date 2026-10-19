#!/usr/bin/env python3
"""Real local-loop benchmark for the live Review developer loop.

Measures the product boundaries that matter to a human reviewer:

* ``first_diff_ready_ms`` -- working-tree capture start -> exact source/targets
  published and the Reader-ready callback fired. Impact enrichment continues
  after this boundary.
* ``warm_navigation_ms`` -- ``lc review --open`` start -> browser navigation on
  an already-prepared exact source generation.
* ``checkpoint_capture_ms`` -- Reader-equivalent ``POST /refresh`` after a new
  source edit -> the new immutable Review revision is returned.
* ``feedback_handoff_accept_ms`` -- exact-session Review feedback adapter start
  -> accepted by the running-session inbox transport.

This is a benchmark, not a CI latency assertion. Run several samples on the
same machine and compare distributions/commits instead of treating one wall
clock number as a correctness gate.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pygit2
from click.testing import CliRunner
from lemoncrow.core.capabilities import plugin_runtime
from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review.delivery import deliver_to_agent_session
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.hosted import capture_server_review, server_review_request
from lemoncrow_client.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Sample:
    first_diff_ready_ms: float
    capture_total_ms: float
    warm_navigation_ms: float
    checkpoint_capture_ms: float
    feedback_handoff_accept_ms: float

    def to_dict(self) -> dict[str, float]:
        return {
            "first_diff_ready_ms": self.first_diff_ready_ms,
            "capture_total_ms": self.capture_total_ms,
            "warm_navigation_ms": self.warm_navigation_ms,
            "checkpoint_capture_ms": self.checkpoint_capture_ms,
            "feedback_handoff_accept_ms": self.feedback_handoff_accept_ms,
        }


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _python_for_local_server() -> Path:
    enterprise = REPO_ROOT / "enterprise" / "server" / ".venv" / "bin" / "python"
    return enterprise if enterprise.is_file() else Path(sys.executable)


def _server_env() -> dict[str, str]:
    env = os.environ.copy()
    root_site = REPO_ROOT / ".venv" / "lib" / "python3.13" / "site-packages"
    paths = (
        root_site,
        REPO_ROOT / "enterprise" / "server" / "src",
        REPO_ROOT / "server" / "src",
        REPO_ROOT / "client" / "src",
        REPO_ROOT / "src",
    )
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths if path.exists())
    return env


@contextlib.contextmanager
def _local_server(root: Path) -> Iterator[tuple[str, Path]]:
    frontend = REPO_ROOT / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        raise RuntimeError("frontend/dist is required; build the Reader before running this benchmark")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    token_file = root / "machine-token"
    log_path = root / "server.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            str(_python_for_local_server()),
            "-m",
            "lemoncrow_server_core",
            "up",
            "--directory",
            str(root / "server"),
            "--port",
            str(port),
            "--token-file",
            str(token_file),
            "--frontend-dir",
            str(frontend),
            "--allow-local-fs",
        ],
        cwd=REPO_ROOT,
        env=_server_env(),
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 12.0
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log.flush()
                raise RuntimeError(f"local Review server exited early; see {log_path}")
            try:
                with urllib.request.urlopen(f"{url}/healthz", timeout=0.25) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.03)
        else:
            raise RuntimeError(f"local Review server did not become healthy; see {log_path}")
        yield url, token_file
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        log.close()


@contextlib.contextmanager
def _client_environment(*, url: str, token_file: Path, store_root: Path) -> Iterator[None]:
    wanted = {
        "LEMONCROW_INSTALL_MODE": "local",
        "LEMONCROW_URL": url,
        "LEMONCROW_TOKEN_FILE": str(token_file),
        "LEMONCROW_LOCAL_FS": "1",
        "LEMONCROW_HOME": str(store_root),
    }
    previous = {name: os.environ.get(name) for name in wanted}
    try:
        os.environ.update(wanted)
        os.environ.pop("LEMONCROW_TOKEN", None)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _fixture_repo(root: Path, *, files: int, changed_files: int) -> Path:
    repo_root = root / "repo"
    repo_root.mkdir(parents=True)
    repo = pygit2.init_repository(str(repo_root), initial_head="main")
    repo.config["user.name"] = "Review Benchmark"
    repo.config["user.email"] = "review-benchmark@localhost"
    package = repo_root / "src"
    package.mkdir()
    for index in range(files):
        (package / f"module_{index:03d}.py").write_text(
            f"def value_{index}():\n    return {index}\n",
            encoding="utf-8",
        )
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    signature = pygit2.Signature("Review Benchmark", "review-benchmark@localhost", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, "benchmark base", tree, [])
    for index in range(min(files, changed_files)):
        (package / f"module_{index:03d}.py").write_text(
            f"def value_{index}():\n    return {index + 1000}\n",
            encoding="utf-8",
        )
    return repo_root


def _measure_warm_navigation(store_root: Path, repo_root: Path) -> float:
    opened_at: list[float] = []
    original_open = webbrowser.open
    started = time.perf_counter()

    def record_open(_url: str, *_args: Any, **_kwargs: Any) -> bool:
        opened_at.append(time.perf_counter())
        return True

    webbrowser.open = record_open
    try:
        result = CliRunner().invoke(
            cli,
            [
                "--root",
                str(store_root),
                "review",
                "--repo-root",
                str(repo_root),
                "--working-tree",
                "--open",
                "--no-impact",
                "--no-provenance",
            ],
            catch_exceptions=False,
        )
    finally:
        webbrowser.open = original_open
    if result.exit_code != 0:
        raise RuntimeError(f"warm lc review failed: {result.output}")
    if not opened_at:
        raise RuntimeError("warm lc review did not attempt browser navigation")
    return (opened_at[0] - started) * 1000.0


def _measure_feedback_handoff(store_root: Path, repo_root: Path) -> float:
    session_id = "benchmark-opencode-session"
    plugin_runtime._write_opencode_session_state(
        store_root,
        {
            "session_id": session_id,
            "cwd": str(repo_root),
            "hook_event_name": "UserPromptSubmit",
        },
        host="opencode",
    )
    bin_dir = store_root / "benchmark-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "opencode"
    shim.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    shim.chmod(0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    try:
        started = time.perf_counter()
        result = deliver_to_agent_session(
            "opencode",
            session_id,
            repo_root,
            "Please keep the invalid-session branch explicit.",
        )
        elapsed = _elapsed_ms(started)
    finally:
        os.environ["PATH"] = old_path
    if not result.accepted or not result.remote_ref.startswith("inbox:"):
        raise RuntimeError(f"feedback handoff did not use exact-session inbox: {result}")
    return elapsed


def run_sample(*, files: int, changed_files: int) -> Sample:
    with tempfile.TemporaryDirectory(prefix="lc-review-live-bench-") as temp:
        root = Path(temp)
        store_root = root / "state"
        repo_root = _fixture_repo(root, files=files, changed_files=changed_files)
        with (
            _local_server(root) as (url, token_file),
            _client_environment(
                url=url,
                token_file=token_file,
                store_root=store_root,
            ),
        ):
            config = load_config(cwd=repo_root)
            rng = resolve_rev_range(repo_root, working_tree=True)
            first_ready: list[float] = []
            started = time.perf_counter()
            captured = capture_server_review(
                config,
                repo_root,
                rng,
                store_root=store_root,
                session_id=None,
                limit=max(files, 40),
                with_impact=True,
                with_provenance=False,
                ready=lambda _capture: first_ready.append(time.perf_counter()),
            )
            total_capture_ms = _elapsed_ms(started)
            if not first_ready:
                raise RuntimeError("progressive capture never fired the source-ready callback")
            first_diff_ready_ms = (first_ready[0] - started) * 1000.0

            warm_navigation_ms = _measure_warm_navigation(store_root, repo_root)

            checkpoint_path = repo_root / "src" / "module_000.py"
            checkpoint_path.write_text("def value_0():\n    return 4242\n", encoding="utf-8")
            started = time.perf_counter()
            refreshed = server_review_request(
                config,
                "POST",
                f"/api/reviews/{captured.review_id}/refresh",
                body={},
            )
            checkpoint_capture_ms = _elapsed_ms(started)
            revision = refreshed.get("revision")
            if not isinstance(revision, dict) or int(revision.get("revision_number") or 0) < 2:
                raise RuntimeError(f"checkpoint refresh did not advance the Review revision: {refreshed}")

            feedback_handoff_accept_ms = _measure_feedback_handoff(store_root, repo_root)
            return Sample(
                first_diff_ready_ms=first_diff_ready_ms,
                capture_total_ms=total_capture_ms,
                warm_navigation_ms=warm_navigation_ms,
                checkpoint_capture_ms=checkpoint_capture_ms,
                feedback_handoff_accept_ms=feedback_handoff_accept_ms,
            )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def summarize(samples: list[Sample]) -> dict[str, Any]:
    metrics = tuple(Sample.__dataclass_fields__)
    summary: dict[str, Any] = {"samples": len(samples), "metrics": {}}
    for metric in metrics:
        values = [float(getattr(sample, metric)) for sample in samples]
        summary["metrics"][metric] = {
            "median_ms": statistics.median(values),
            "p95_ms": _percentile(values, 0.95),
            "min_ms": min(values),
            "max_ms": max(values),
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=3, help="Independent repo/server samples (default: 3).")
    parser.add_argument("--files", type=int, default=40, help="Files in the fixture repository (default: 40).")
    parser.add_argument("--changed-files", type=int, default=8, help="Initially changed files (default: 8).")
    parser.add_argument("--json", action="store_true", help="Emit only machine-readable JSON.")
    args = parser.parse_args(argv)
    if args.samples < 1 or args.files < 1 or args.changed_files < 1:
        parser.error("samples, files and changed-files must all be positive")

    samples = [run_sample(files=args.files, changed_files=args.changed_files) for _ in range(args.samples)]
    payload = {
        "benchmark": "review-live-runtime",
        "commit": subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip(),
        "fixture": {"files": args.files, "changed_files": args.changed_files},
        "runs": [sample.to_dict() for sample in samples],
        "summary": summarize(samples),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0

    print(f"Review live-runtime benchmark · {payload['commit']} · {args.samples} sample(s)")
    print(f"Fixture: {args.files} files · {args.changed_files} changed")
    for name, row in payload["summary"]["metrics"].items():
        print(f"{name:<28} median {row['median_ms']:8.1f} ms   p95 {row['p95_ms']:8.1f} ms")
    print("\nJSON")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
