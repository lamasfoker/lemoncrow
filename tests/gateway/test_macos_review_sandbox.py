from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.pro.capabilities.review import host_tools, runtimes, web_preview
from lemoncrow.pro.capabilities.review.macos_sandbox import sandbox_command
from lemoncrow.pro.capabilities.review.sandbox_limits import run_limited
from lemoncrow.pro.capabilities.review.surfaces import RuntimeConfigEntry


@pytest.mark.parametrize(
    "name,target",
    [
        ("docker", "/Applications/Docker.app/Contents/Resources/bin/docker"),
        ("npm", "/opt/homebrew/bin/npm"),
        ("docker-compose", "/Applications/Docker.app/Contents/Resources/cli-plugins/docker-compose"),
    ],
)
def test_macos_tools_without_shell_path(name: str, target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(host_tools.shutil, "which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda self: str(self) == target)
    monkeypatch.setattr(os, "access", lambda path, mode: str(path) == target)
    assert host_tools.host_executable(name) == target
    if name == "npm":
        assert web_preview._sandbox_visible_executable(name)


requires_macos = pytest.mark.skipif(sys.platform != "darwin", reason="requires native macOS Seatbelt")


@requires_macos
def test_macos_npm_build_is_confined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not host_tools.host_executable("npm"):
        pytest.skip("npm is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    modules = tmp_path / "checkout" / "node_modules"
    modules.mkdir(parents=True)
    dependency = modules / "borrowed.txt"
    dependency.write_text("original")
    secret = tmp_path / "secret"
    secret.write_text("private")
    monkeypatch.setenv("REVIEW_TEST_SECRET", "must-not-reach-build")
    (workspace / "package.json").write_text(json.dumps({"scripts": {"build": "node build.cjs"}}))
    (workspace / "build.cjs").write_text(
        "const fs=require('fs');"
        "if(process.env.REVIEW_TEST_SECRET)throw Error('environment leaked');"
        f"for(const p of {json.dumps([str(dependency), str(secret)])}){{"
        "try{fs.writeFileSync(p,'changed');throw Error('write allowed')}catch(e){"
        "if(!['EPERM','EACCES'].includes(e.code))throw e}}"
        f"try{{fs.readFileSync({json.dumps(str(secret))});throw Error('read allowed')}}"
        "catch(e){if(!['EPERM','EACCES'].includes(e.code))throw e}"
        "fs.mkdirSync('dist');fs.writeFileSync('dist/index.html','preview');"
    )
    command = web_preview._macos_build_command(
        workspace,
        modules,
        repo_root=modules.parent,
        project_root=".",
        package_manager="npm",
        framework="vite",
    )
    proc = run_limited(command, timeout=30, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (workspace / "dist/index.html").read_text() == "preview"
    assert dependency.read_text() == "original"
    assert secret.read_text() == "private"


@requires_macos
def test_docker_compose_runner_with_cleared_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not host_tools.host_executable("docker-compose"):
        pytest.skip("Docker Compose is not installed")
    context = SimpleNamespace(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        revision=SimpleNamespace(review_id="review", id="revision"),
    )

    def materialize(*args: object, target: Path, **kwargs: object) -> str:
        target.mkdir(parents=True)
        (target / "compose.yaml").write_text("services:\n  web:\n    image: nginx:alpine\n")
        return "identity"

    monkeypatch.setattr(runtimes, "materialize_review_side", materialize)
    runtime = RuntimeConfigEntry("compose", "docker-compose", {"compose": ["compose.yaml"]})
    surface = SimpleNamespace(id="web", provider="service", kind="service", metadata={"service": "web"})
    result = runtimes.DockerComposeRunner().run(context, runtime, surface, side="new")
    assert result.status == "passed", result.output or result.summary


@requires_macos
def test_macos_vite_build_with_read_only_checkout_dependencies(tmp_path: Path) -> None:
    checkout = Path(__file__).resolve().parents[2] / "frontend"
    modules = checkout / "node_modules"
    if not (modules / "vite").is_dir():
        pytest.skip("frontend Vite dependencies are not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}, "type": "module"}))
    (workspace / "index.html").write_text('<div id="app"></div><script type="module" src="/main.js"></script>')
    (workspace / "main.js").write_text("document.querySelector('#app').textContent='review';")
    (workspace / "vite.config.js").write_text("export default { build: { minify: false } };")
    command = web_preview._macos_build_command(
        workspace,
        modules,
        repo_root=checkout.parent,
        project_root=".",
        package_manager="npm",
        framework="vite",
    )
    result = run_limited(command, timeout=30, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (workspace / "dist/index.html").is_file()


@requires_macos
@pytest.mark.parametrize("bind", ["127.0.0.1", "0.0.0.0"])
def test_macos_http_supervisor_and_network_confinement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bind: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    (workspace / "app.py").write_text(
        "import http.server,os\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200);self.end_headers();self.wfile.write(b'{\"ok\":true}')\n"
        f"http.server.HTTPServer(({bind!r},int(os.environ['PORT'])),Handler).serve_forever()\n"
    )
    config = {
        "command": [sys.executable, "app.py"],
        "port": port,
        "port_env": "PORT",
        "health": "/health",
        "health_timeout": 5,
        "request_timeout": 2,
        "max_body_bytes": 256000,
        "method": "GET",
        "path": "/hello",
    }
    kwargs = dict(
        workspace=workspace,
        cwd=workspace,
        executables=(sys.executable,),
        read_roots=(Path(sys.base_prefix),),
        loopback_port=port,
    )
    command = sandbox_command(
        [sys.executable, "-c", runtimes._HTTP_SERVICE_SUPERVISOR, json.dumps(config)],
        **kwargs,
    )
    proc = run_limited(command, timeout=15, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["body"] == {"ok": True}
    context = SimpleNamespace(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        revision=SimpleNamespace(review_id="review", id="revision"),
    )

    def materialize(*args: object, target: Path, **kwargs: object) -> str:
        target.mkdir(parents=True)
        (target / "app.py").write_text((workspace / "app.py").read_text())
        return "identity"

    monkeypatch.setattr(runtimes, "materialize_review_side", materialize)
    runtime = RuntimeConfigEntry("api", "http-service", {"command": [sys.executable, "app.py"], "port": port})
    surface = SimpleNamespace(id="hello", provider="bruno", kind="api.request", metadata={"url": "/hello"})
    result = runtimes.HttpServiceRunner().run(context, runtime, surface, side="new")
    assert result.status == "passed", result.output or result.summary
    assert result.data["body"] == {"ok": True}
    probe = (
        "import pathlib,socket\n"
        "try:\n pathlib.Path('app.py').write_text('changed')\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('snapshot write allowed')\n"
        "s=socket.socket()\n"
        "try:\n s.connect(('127.0.0.1',1))\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('other loopback port allowed')\n"
    )
    result = subprocess.run(
        sandbox_command([sys.executable, "-c", probe], **kwargs), capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


@requires_macos
@pytest.mark.parametrize("side", ["old", "new"])
def test_macos_command_runner_confines_reviewed_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    secret = tmp_path / "secret"
    secret.write_text("private")
    context = SimpleNamespace(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        revision=SimpleNamespace(review_id="review", id="revision"),
    )

    def materialize(*args: object, target: Path, **kwargs: object) -> str:
        target.mkdir(parents=True)
        return "identity"

    monkeypatch.setattr(runtimes, "materialize_review_side", materialize)
    monkeypatch.setenv("REVIEW_TEST_SECRET", "private")
    script = (
        "import pathlib,os,socket\n"
        "assert 'REVIEW_TEST_SECRET' not in os.environ\n"
        f"try:\n pathlib.Path({str(secret)!r}).read_text()\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('host read allowed')\n"
        "s=socket.socket()\n"
        "try:\n s.connect(('127.0.0.1',1))\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('network allowed')\n"
        "pathlib.Path('result.txt').write_text('passed')\n"
        "print('check passed')\n"
    )
    runtime = RuntimeConfigEntry("checks", "command", {"command": ["python3", "-c", script]})
    surface = SimpleNamespace(id="checks", provider="unit-tests")
    result = runtimes.CommandRunner().run(context, runtime, surface, side=side)
    assert result.status == "passed", result.output or result.summary
    assert "check passed" in result.output


@requires_macos
def test_macos_service_startup_includes_failure_log(tmp_path: Path) -> None:
    config = {
        "command": [sys.executable, "-c", "raise RuntimeError('startup detail')"],
        "port": 8765,
        "port_env": "PORT",
        "health": "/health",
        "health_timeout": 3,
    }
    command = sandbox_command(
        [sys.executable, "-c", runtimes._HTTP_SERVICE_SUPERVISOR, json.dumps(config)],
        workspace=tmp_path,
        cwd=tmp_path,
        executables=(sys.executable,),
        loopback_port=8765,
    )
    proc = run_limited(command, timeout=10, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "startup detail" in proc.stderr
    assert "service exited before health check" in proc.stderr


@requires_macos
def test_macos_pixel_capture_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        web_preview._chrome_executable()
    except web_preview.WebPreviewUnavailable:
        pytest.skip("Chrome Headless Shell is not installed")
    output = tmp_path / "workspace" / "dist"
    output.mkdir(parents=True)
    (output / "index.html").write_text("<style>body{background:rgb(12,34,56)}</style><h1>Frozen review</h1>")
    revision = SimpleNamespace(id="revision", review_id="review", base_sha="base", head_sha="head", mode="commit_range")
    descriptor = web_preview.WebPreviewDescriptor(framework="vite", routes=("/",), default_route="/")
    monkeypatch.setattr(web_preview, "infer_web_preview_descriptor", lambda *args, **kwargs: descriptor)
    monkeypatch.setattr(web_preview, "_ensure_built", lambda *args, **kwargs: output)
    monkeypatch.setattr(web_preview, "_build_cache_root", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(web_preview, "_prune_web_preview_cache", lambda *args, **kwargs: None)
    image = web_preview.render_web_preview(
        tmp_path,
        tmp_path,
        revision,
        path="src/main.js",
        route="/",
        side="old",
        width=640,
        height=480,
    )
    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert image.stat().st_size > 1000
    assert (
        web_preview.render_web_preview(
            tmp_path,
            tmp_path,
            revision,
            path="src/main.js",
            route="/",
            side="old",
            width=640,
            height=480,
        )
        == image
    )


@requires_macos
def test_macos_concurrent_service_sides_use_distinct_ports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    context = SimpleNamespace(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        revision=SimpleNamespace(review_id="review", id="revision"),
    )

    def materialize(*args: object, target: Path, side: str, **kwargs: object) -> str:
        target.mkdir(parents=True)
        (target / "app.py").write_text(
            "import http.server,os,json,time\n"
            "class Handler(http.server.BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  time.sleep(.1)\n"
            f"  body=json.dumps({{'side':{side!r},'port':int(os.environ['PORT'])}}).encode()\n"
            "  self.send_response(200);self.end_headers();self.wfile.write(body)\n"
            "http.server.ThreadingHTTPServer(('0.0.0.0',int(os.environ['PORT'])),Handler).serve_forever()\n"
        )
        return "identity"

    monkeypatch.setattr(runtimes, "materialize_review_side", materialize)
    # A live unrelated listener occupies the configured port throughout.
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        configured_port = occupied.getsockname()[1]
        runtime = RuntimeConfigEntry(
            "api",
            "http-service",
            {"command": ["python3", "app.py"], "port": configured_port},
        )
        jobs = [(surface, side) for surface in ("plans", "summary") for side in ("old", "new")]

        def run(job: tuple[str, str]) -> object:
            surface_id, side = job
            surface = SimpleNamespace(
                id=surface_id,
                provider="bruno",
                kind="api.request",
                metadata={"url": "/" + surface_id},
            )
            return runtimes.HttpServiceRunner().run(context, runtime, surface, side=side)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(run, jobs))
        for result, (_, side) in zip(results, jobs, strict=True):
            assert result.status == "passed", result.output or result.summary
            assert result.data["body"]["side"] == side
            assert result.data["body"]["port"] != configured_port
        assert len({result.data["body"]["port"] for result in results}) == 4
    assert not runtimes._MACOS_SERVICE_PORTS


def test_repo_named_executables_never_open_roots_under_the_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_home = tmp_path / "home"
    docker = user_home / ".docker" / "bin" / "docker"
    npm = tmp_path / "opt" / "toolchain" / "bin" / "npm"
    for tool in (docker, npm):
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/sh\n")
    (user_home / ".docker" / "config.json").write_text('{"auths": {}}')
    found = {"sandbox-exec": "/usr/bin/sandbox-exec", "docker": str(docker), "npm": str(npm)}
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.macos_sandbox.host_executable", found.get)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def profile(repo_executable: str) -> str:
        argv = sandbox_command(
            [repo_executable], workspace=workspace, cwd=workspace, repo_executables=(repo_executable,)
        )
        return argv[argv.index("-p") + 1]

    assert str(user_home) not in profile("docker")
    assert str(npm.parent.parent) in profile("npm")
    # LemonCrow's own toolchain names keep their roots wherever they live.
    argv = sandbox_command(["docker"], workspace=workspace, cwd=workspace, executables=("docker",))
    assert str(user_home / ".docker") in argv[argv.index("-p") + 1]
