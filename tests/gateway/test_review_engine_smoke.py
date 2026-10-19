"""The shipped Review-only engine works end to end, proven from its built artifact (G2-ENGINE).

Builds the release artifact with ``scripts/build_review_engine.sh`` into a temporary
directory, installs its wheel into a clean venv, starts it the way
``scripts/local_server.sh`` does, captures a Review of a temporary git repository,
then opens it, lists the Inbox and reads analytics through the HTTP API, and proves
the Runtime implementation is absent from the installed engine.

The capture runs the isolated Review client the Review-only install ships: built by
``scripts/build_review_client.sh`` and installed by ``scripts/hosted.sh`` against the
engine's loopback URL, as ``scripts/local_review.sh`` does. Its ``lcr`` syncs the
change's base and head as Views and the engine derives the Review packet from them.

Run it (``slow``: excluded from the default run)::

    uv run pytest -m slow tests/gateway/test_review_engine_smoke.py -q

Network: none with a warm uv cache. With a cold cache uv downloads the wheel build
backend (``hatchling``) and the engine's dependencies (aiohttp, fastapi, pydantic,
pygit2, pyyaml, regex and what they depend on) from the package index. Set
``UV_OFFLINE=1`` to forbid that; the test then fails instead of downloading.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import selectors
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import pytest

from lemoncrow.pro.capabilities.review.store import DB_NAME, ReviewStore

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "scripts" / "build_review_engine.sh"
CLIENT_BUILDER = ROOT / "scripts" / "build_review_client.sh"
CLIENT_INSTALLER = ROOT / "scripts" / "hosted.sh"

#: Runtime implementation that must not be importable from the installed engine.
RUNTIME_MODULES = (
    "lemoncrow.gateway",
    "lemoncrow.pro.capabilities.code_context",
    "lemoncrow.pro.capabilities.model_routing",
    "lemoncrow.pro.capabilities.memory",
    "lemoncrow.pro.capabilities.swarm",
    "lemoncrow_client.mcpserver",
    "lemoncrow_client.dispatcher",
    "lemoncrow_client.session",
    "lemoncrow_client.routing",
    "lemoncrow_client.localtools",
    "lemoncrow_server_core.registry_dispatch",
    "lemoncrow_server_core.product_telemetry",
    "lemoncrow_server_core.tracing",
    "lemoncrow_server_core.dashboard_service",
    "lemoncrow_server_core.runtime_product_surface",
)

#: A remote image the Review's Markdown document references (never fetched: see below).
_REMOTE_IMAGE = "https://example.com/diagram.png"

#: The Reader's first-load reads after the Review payload itself (as the browser asks).
_READER_FIRST_LOAD = ("/revisions", "/targets", "/readiness", "/projection", "/annotations", "/destination/options")

_ABSENCE_PROBE = r"""
import importlib.util, json, sys
found = []
for name in sys.argv[1:]:
    try:
        spec = importlib.util.find_spec(name)
    except ModuleNotFoundError:
        spec = None
    if spec is not None:
        found.append(name)
import lemoncrow_server_core
print(json.dumps({"found": found, "server_core": lemoncrow_server_core.__file__}))
"""

# Inherited by the build and install steps only: uv's own settings (cache, index,
# UV_OFFLINE) plus what a shell needs. Nothing LemonCrow-specific leaks in.
_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")


class _Env(dict[str, str]):
    """A subprocess environment whose repr (in a failure report) names variables, never values."""

    def __repr__(self) -> str:
        return f"<environment: {', '.join(sorted(self))}>"


def _tool_env(tmp: Path, **extra: str) -> _Env:
    env = _Env({name: os.environ[name] for name in _PASSTHROUGH if name in os.environ})
    env.update({name: value for name, value in os.environ.items() if name.startswith("UV_")})
    env["TMPDIR"] = str(tmp)
    env.update(extra)
    return env


def _isolated_env(home: Path, **extra: str) -> _Env:
    """A process environment with a private HOME and no inherited LemonCrow settings."""

    env = _Env({name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL") if name in os.environ})
    env.update({"HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
    env.update(extra)
    return env


def _tail(log: Path | None) -> str:
    return "" if log is None or not log.is_file() else "\nengine log:\n" + log.read_text(encoding="utf-8")[-8000:]


def _run(cmd: list[str], *, env: _Env, cwd: Path | None = None, timeout: float = 600, log: Path | None = None) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout, check=False)
    assert (
        done.returncode == 0
    ), f"{Path(cmd[0]).name} exited {done.returncode}:\n{done.stdout[-3000:]}\n{done.stderr[-6000:]}{_tail(log)}"
    return done.stdout


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repository(root: Path) -> Path:
    """A repository with a committed base and a working-tree change in Python, TypeScript and Markdown."""

    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "web").mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "engine@example.com")
    _git(repo, "config", "user.name", "Engine Smoke")
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "maths.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "web" / "app.ts").write_text("export function greet(name: string) {\n  return name;\n}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "pkg" / "maths.py").write_text(
        "from pkg import maths\n\n\ndef add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return add(a, -b)\n",
        encoding="utf-8",
    )
    (repo / "web" / "app.ts").write_text(
        "export function greet(name: string) {\n  return `hello ${name}`;\n}\n", encoding="utf-8"
    )
    (repo / "README.md").write_text(f"# Maths\n\n![diagram]({_REMOTE_IMAGE})\n", encoding="utf-8")
    return repo


def _build(tmp: Path) -> tuple[Path, Path]:
    """Build the release artifact; return its engine wheel and its frontend directory."""

    stub = tmp / "frontend-stub"
    stub.mkdir()
    (stub / "index.html").write_text("<!doctype html><title>LemonCrow Review</title>\n", encoding="utf-8")
    dist = tmp / "dist"
    _run(
        ["bash", str(BUILDER)],
        env=_tool_env(
            tmp,
            LEMONCROW_REVIEW_ENGINE_DIST_DIR=str(dist),
            LEMONCROW_REVIEW_ENGINE_FRONTEND_DIR=str(stub),
        ),
        cwd=ROOT,
    )
    unpacked = tmp / "artifact"
    with tarfile.open(dist / "lemoncrow-review-engine.tar.gz", "r:gz") as archive:
        archive.extractall(unpacked, filter="data")
    [wheel] = sorted((unpacked / "vendor").glob("lemoncrow_review_engine-*-py3-none-any.whl"))
    return wheel, unpacked / "frontend"


def _install(tmp: Path, wheel: Path) -> Path:
    venv = tmp / "venv"
    env = _tool_env(tmp)
    _run(["uv", "venv", "--python", f"{sys.version_info.major}.{sys.version_info.minor}", str(venv)], env=env)
    _run(["uv", "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel)], env=env)
    return venv


def _build_client(tmp: Path) -> Path:
    """Build the isolated Review client artifact; return its archive."""

    dist = tmp / "client-dist"
    _run(["bash", str(CLIENT_BUILDER)], env=_tool_env(tmp, LEMONCROW_REVIEW_DIST_DIR=str(dist)), cwd=ROOT)
    return dist / "lemoncrow-review-client.tar.gz"


def _install_client(tmp: Path, archive: Path, url: str, token: str) -> tuple[Path, _Env]:
    """Install the Review client against the engine as local_review.sh does; return ``lcr`` and its environment.

    The engine's token goes where a local install keeps it (``$LEMONCROW_HOME/token``).
    """

    home = tmp / "review-home"
    env = _isolated_env(tmp / "review-user", LEMONCROW_HOME=str(home), TMPDIR=str(tmp))
    _run(
        ["bash", str(CLIENT_INSTALLER), "--from-build", str(archive), "--url", url],
        env=_Env({**env, "LEMONCROW_BIN_DIR": str(home / "bin")}),
    )
    token_file = home / "token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    return home / "bin" / "lcr", env


@dataclass(frozen=True)
class _Engine:
    url: str
    token: str
    log: Path

    def get(self, path: str) -> tuple[int, Any]:
        request = urllib.request.Request(
            self.url + path, headers={"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                status, body, kind = response.status, response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            status, body, kind = exc.code, exc.read(), exc.headers.get_content_type()
        payload: Any = json.loads(body) if kind == "application/json" else body.decode("utf-8", "replace")
        return status, payload

    def ok(self, path: str) -> Any:
        status, payload = self.get(path)
        assert status == 200, f"GET {path} -> {status}: {str(payload)[:3000]}{_tail(self.log)}"
        return payload


@contextmanager
def _engine(venv: Path, state: Path, frontend: Path, home: Path, log: Path) -> Iterator[_Engine]:
    """Start the installed engine the way scripts/local_server.sh does; yield it; stop it cleanly.

    Same flags as the launcher (persistent index, token file, local file access), on an
    ephemeral port so parallel runs never collide.
    """

    token = secrets.token_hex(32)
    token_file = home / "token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    with log.open("w", encoding="utf-8") as sink:
        process = subprocess.Popen(
            [
                str(venv / "bin" / "python"),
                "-m",
                "lemoncrow_server_core",
                "up",
                "--directory",
                str(state),
                "--port",
                "0",
                "--token-file",
                str(token_file),
                "--frontend-dir",
                str(frontend),
                "--allow-local-fs",
            ],
            stdout=subprocess.PIPE,
            stderr=sink,
            text=True,
            env=_isolated_env(home, LEMONCROW_LOCAL_SERVER_PRODUCTS="review"),
        )
        try:
            assert process.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            ready = selector.select(timeout=120)
            url = process.stdout.readline().strip() if ready else ""
            assert url.startswith("http://127.0.0.1:"), f"engine did not start{_tail(log)}"
            yield _Engine(url=url, token=token, log=log)
        finally:
            process.send_signal(signal.SIGTERM)
            try:
                code = process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait(timeout=10)
    assert code == 0, f"engine exited {code}{_tail(log)}"


def test_the_built_review_engine_captures_opens_lists_and_reports_a_review(tmp_path: Path) -> None:
    started = time.monotonic()
    wheel, frontend = _build(tmp_path)
    # The engine records the frontend it packaged: here the stub, an override no
    # release may ship.
    with zipfile.ZipFile(wheel) as archive:
        record = json.loads(archive.read("lemoncrow_server_core/review_engine_build.json"))
    assert record == {
        "schema": 1,
        "frontend": {
            "source": "LEMONCROW_REVIEW_ENGINE_FRONTEND_DIR",
            "override": True,
            "index_sha256": hashlib.sha256((frontend / "index.html").read_bytes()).hexdigest(),
        },
    }
    venv = _install(tmp_path, wheel)
    client_archive = _build_client(tmp_path)
    engine_home = tmp_path / "engine-home"
    engine_home.mkdir()

    # The Runtime implementation is not installed: nothing of it is importable.
    probe = json.loads(
        _run(
            [str(venv / "bin" / "python"), "-I", "-c", _ABSENCE_PROBE, *RUNTIME_MODULES],
            env=_isolated_env(engine_home),
            cwd=tmp_path,
        )
    )
    assert probe["found"] == []
    assert Path(probe["server_core"]).is_relative_to(venv), "the probe must import the installed engine"

    repo = _repository(tmp_path)
    state = tmp_path / "state"
    log = tmp_path / "engine.log"
    with _engine(venv, state, frontend, engine_home, log) as engine:
        identity = engine.ok("/.well-known/lemoncrow-server.json")
        assert identity["products"]["review"]["available"] is True
        assert identity["products"]["runtime"]["available"] is False

        # Capture the working-tree change with the installed isolated Review client.
        lcr, lcr_env = _install_client(tmp_path, client_archive, engine.url, engine.token)
        out = _run([str(lcr), "--working-tree", "--no-open", "--json"], env=lcr_env, cwd=repo, log=log)
        captured = json.loads(out.strip().splitlines()[-1])
        review_id, repo_id = str(captured["review"]["id"]), str(captured["review"]["repo_id"])
        assert review_id and repo_id
        assert captured["review_url"] == engine.url + captured["review"]["review_path"]

        # The capture created the Review store, with every migration the store declares.
        [database] = sorted(state.rglob(DB_NAME))
        with closing(sqlite3.connect(database)) as conn:
            applied = {row[0] for row in conn.execute("SELECT name FROM _schema_migrations")}
        assert applied == set(ReviewStore.MIGRATIONS)

        # Open it: the Review row, the Reader's payload and first-load reads, the browser route.
        row = engine.ok(f"/v1/repos/{quote(repo_id, safe='')}/reviews/{quote(review_id, safe='')}")
        assert str(row["review"]["id"]) == review_id
        reader = f"/api/reviews/{quote(review_id, safe='')}"
        opened = engine.ok(reader)
        assert opened
        for suffix in _READER_FIRST_LOAD:
            engine.ok(reader + suffix)
        page = engine.ok(str(row["review"]["review_path"]))
        assert "LemonCrow Review" in page

        # Reader features: shipped ones answer, left-out ones say so (never a 500).
        assert engine.ok(reader + "/map")
        assert engine.ok(reader + "/journal")
        assert engine.ok(reader + "/measures")
        assert engine.ok(reader + "/surfaces")
        delta = engine.ok(reader + "/map?projection=architecture_delta")["architecture_delta"]
        assert delta["status"] == "unavailable"
        assert "repository map" in json.dumps(delta)
        image = engine.get(
            reader + "/markdown-image?" + urlencode({"document_path": "README.md", "side": "new", "src": _REMOTE_IMAGE})
        )
        assert image == (501, {"detail": "remote Markdown images are not available in this build"}), image

        # The Inbox: the Reviews list and its view mode both list the Review.
        listed = engine.ok("/v1/reviews")
        assert review_id in {str(item["id"]) for item in listed["reviews"]}
        inbox = engine.ok("/v1/reviews?view=all")
        assert inbox["mode"] == "views"
        assert review_id in {str(item["id"]) for item in inbox["reviews"]}

        # Analytics count the Review and name, never fail on, what this engine does not ship.
        report = engine.ok("/api/review-analytics")
        assert report["volume"]["reviews"]["count"] == 1
        assert all(item["source"] and item["sections"] for item in report["unavailable"])
        served = log.read_text(encoding="utf-8")

    # Nothing failed out of sight while serving: no traceback before shutdown, and no
    # missing module ever (a capture's best-effort warm-up may still be reading when the
    # engine stops, so a read failing during shutdown is not this test's concern).
    assert "Traceback" not in served, served[-8000:]
    logged = log.read_text(encoding="utf-8")
    assert "ModuleNotFoundError" not in logged and "ImportError" not in logged, logged[-8000:]
    print(f"engine smoke: {time.monotonic() - started:.1f}s")
