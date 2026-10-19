from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import lemoncrow_client
import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the Pi extension contract")
@pytest.mark.parametrize("installed", [False, True])
def test_pi_lite_extension_registers_and_proxies_exact_lite_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: bool
) -> None:
    asset = Path(lemoncrow_client.__file__).resolve().parent / "assets" / "pi_lite.mjs"
    fake_mcp = tmp_path / "fake_mcp.py"
    fake_mcp.write_text(
        """import json, sys\ntools = [\n    {\"name\": name, \"description\": name, \"inputSchema\": {\"type\": \"object\", \"additionalProperties\": True}}\n    for name in (\"bash\", \"code_search\", \"edit\", \"read\", \"web_fetch\")\n]\nfor line in sys.stdin:\n    msg = json.loads(line)\n    method = msg.get(\"method\")\n    if method == \"initialize\": result = {\"serverInfo\": {\"name\": \"fake-lite\"}}\n    elif method == \"tools/list\": result = {\"tools\": tools}\n    elif method == \"tools/call\":\n        params = msg.get(\"params\", {})\n        result = {\"content\": [{\"type\": \"text\", \"text\": json.dumps(params, sort_keys=True)}], \"isError\": False}\n    else: result = {}\n    print(json.dumps({\"jsonrpc\": \"2.0\", \"id\": msg.get(\"id\"), \"result\": result}), flush=True)\n""",
        encoding="utf-8",
    )
    runner = tmp_path / "runner.mjs"
    runner.write_text(
        """import { pathToFileURL } from 'node:url';\nconst extension = (await import(pathToFileURL(process.argv[2]).href)).default;\nconst tools = new Map();\nconst handlers = new Map();\nconst pi = {\n  registerTool(tool) { tools.set(tool.name, tool); },\n  on(event, handler) { handlers.set(event, handler); },\n};\nawait extension(pi);\nconst names = [...tools.keys()].sort();\nconst expected = ['bash','code_search','edit','read','web_fetch'].sort();\nif (JSON.stringify(names) !== JSON.stringify(expected)) throw new Error(`tools=${JSON.stringify(names)}`);\nconst result = await tools.get('code_search').execute('call-1', {query: 'needle'});\nif (!result.content?.[0]?.text?.includes('needle')) throw new Error(JSON.stringify(result));\nif (handlers.has('session_shutdown')) handlers.get('session_shutdown')({}, {});\nconsole.log(JSON.stringify(names));\n""",
        encoding="utf-8",
    )
    env = {
        **__import__("os").environ,
        "LEMONCROW_PI_LITE_COMMAND": sys.executable,
        "LEMONCROW_PI_LITE_ARGS": json.dumps([str(fake_mcp)]),
    }
    if installed:
        from lemoncrow_client import host_trial, pi_integration

        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
        monkeypatch.setattr(host_trial, "_runtime_mcp_invocation", lambda _host: (sys.executable, [str(fake_mcp)]))
        settings = pi_integration.install()
        asset = Path(json.loads(settings.read_text())["extensions"][0])
        env.pop("LEMONCROW_PI_LITE_COMMAND", None)
        env.pop("LEMONCROW_PI_LITE_ARGS", None)
    completed = subprocess.run(
        [shutil.which("node") or "node", str(runner), str(asset)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == ["bash", "code_search", "edit", "read", "web_fetch"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the Pi extension contract")
def test_installed_pi_extension_lets_pi_start_when_lite_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed copy loads for every Pi launch; Pi aborts if it throws."""
    from lemoncrow_client import host_trial, pi_integration

    failing = tmp_path / "failing_mcp.py"
    failing.write_text("import sys\nsys.exit('Runtime Lite requires a Git worktree')\n", encoding="utf-8")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setattr(host_trial, "_runtime_mcp_invocation", lambda _host: (sys.executable, [str(failing)]))
    settings = pi_integration.install()
    asset = Path(json.loads(settings.read_text())["extensions"][0])
    runner = tmp_path / "runner.mjs"
    runner.write_text(
        "import { pathToFileURL } from 'node:url';\n"
        "const extension = (await import(pathToFileURL(process.argv[2]).href)).default;\n"
        "const tools = [];\n"
        "await extension({ registerTool(tool) { tools.push(tool.name); }, on() {} });\n"
        "console.log(JSON.stringify(tools));\n",
        encoding="utf-8",
    )
    env = {key: value for key, value in __import__("os").environ.items() if not key.startswith("LEMONCROW_PI_LITE_")}
    completed = subprocess.run(
        [shutil.which("node") or "node", str(runner), str(asset)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == []
    assert "Git worktree" in completed.stderr
