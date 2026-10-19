"""G11-RELEASE compiled-behavior slice, driven against an INSTALLED candidate (mypyc wheel).

Every suite runs the interpreted source. A mypyc-compiled function keeps its parameter names but
not its annotations, so what is built from annotations at registration time (each ``@mcp_tool``'s
input schema and argument coercion) silently differs in the shipped wheel. This drives the
installed ``lc`` the way a client does and checks, on the compiled artifact:

* the ``@mcp_tool`` registry the installed modules build (the candidate's own interpreter): every
  tool's input schema is typed (no ``Any`` property), ``grep.after`` is an integer, and a call whose
  integer arrives as a string (``after="20"``) through the JSON-RPC dispatch returns exactly what
  ``after=20`` returns, and differs from ``after=0`` (the argument is honoured);
* what ``lc runtime mcp`` serves over stdio: typed schemas, and ``bash(timeout="20")`` answered
  like ``timeout=20``;
* the candidate's local Review server answers ``/healthz``, and ``lc review --track`` creates a
  Review and then a second Revision of the same Review through its routes.

Standard library only; run with ``python3 -I``. Exit 0 only when every check holds.
``tests/gateway/test_mcp_compiled_so.py`` runs the registry check on a freshly built wheel.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

TYPED = frozenset({"type", "anyOf", "oneOf", "allOf", "$ref", "enum", "const"})
#: What mypyc says when a compiled function receives an argument of the wrong runtime type.
MYPYC_TYPE_ERROR = re.compile(r"object expected; got|has incompatible type", re.IGNORECASE)
SAMPLE = "def alpha():\n    return 'beta'\n\n\ndef gamma():\n    return 'delta'\n"


def _rpc(rid: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def mcp_session(lc: str, workspace: Path, calls: list[dict[str, Any]], timeout: float = 300) -> dict[int, Any]:
    """Run ``lc runtime mcp`` over stdio: initialize, then *calls*; responses by id."""

    initialize = _rpc(
        1,
        "initialize",
        {"protocolVersion": "2024-11-05", "clientInfo": {"name": "release-slice", "version": "1"}, "capabilities": {}},
    )
    payload = "\n".join(json.dumps(item) for item in (initialize, *calls)) + "\n"
    env = {**os.environ, "CLAUDE_WORKSPACE_ROOT": str(workspace)}
    done = subprocess.run(
        [lc, "runtime", "mcp"],
        input=payload,
        capture_output=True,
        text=True,
        cwd=workspace,
        env=env,
        timeout=timeout,
        check=False,
    )
    responses: dict[int, Any] = {}
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        message = json.loads(line)  # a non-protocol line on stdout is itself a failure
        if isinstance(message, dict) and isinstance(message.get("id"), int):
            responses[message["id"]] = message
    if 1 not in responses:
        raise RuntimeError(f"lc runtime mcp did not initialize (exit {done.returncode}):\n{done.stderr[-2000:]}")
    return responses


def _text(response: dict[str, Any]) -> str:
    if "error" in response:
        raise AssertionError(f"tool call failed: {response['error']}")
    result = response.get("result") or {}
    if result.get("isError"):
        raise AssertionError(f"tool call returned an error: {result}")
    return "".join(str(item.get("text", "")) for item in result.get("content") or () if isinstance(item, dict))


#: Run by the candidate's own interpreter: the ``@mcp_tool`` registry as the installed (compiled)
#: modules build it, and one tool call through the server's JSON-RPC dispatch.
REGISTRY_PROBE = r"""
import json, sys
from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import framework

workspace = sys.argv[1]

def grep(after):
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "grep", "arguments": {"path": workspace, "regex": "beta", "after": after}}}
    return mcp_server._handle(request)

print(json.dumps({
    "framework": framework.__file__,
    "tools": {name: (spec.get("inputSchema") or {}).get("properties") or {} for name, spec in mcp_server.TOOLS.items()},
    "typed": grep(20), "coerced": grep("20"), "none": grep(0),
}, default=str))
"""


def _schema_problems(where: str, tools: dict[str, dict[str, Any]]) -> list[str]:
    return [
        f"{where} {name}.{prop} has an untyped input schema {schema} (annotations lost)"
        for name, properties in sorted(tools.items())
        for prop, schema in properties.items()
        if not (set(schema) & TYPED)
    ]


def _coercion_problems(where: str, typed: dict[str, Any], coerced: dict[str, Any], other: dict[str, Any]) -> list[str]:
    """``coerced`` (a stringified integer) must equal ``typed`` and differ from ``other``."""

    message = json.dumps(coerced.get("error") or coerced.get("result") or {})
    if MYPYC_TYPE_ERROR.search(message):
        return [f"{where}: the stringified integer reached the compiled handler uncoerced: {message[:400]}"]
    try:
        texts = [_text(item) for item in (typed, coerced, other)]
    except AssertionError as exc:
        return [f"{where}: {str(exc)[:600]}"]
    problems = []
    if texts[1] != texts[0]:
        problems.append(f"{where}: the stringified integer was not coerced (its result differs from the typed call)")
    if other and texts[2] == texts[0]:
        problems.append(f"{where}: the integer argument is not honoured (same result as a different value)")
    return problems


def check_registry(python: str, workspace: Path, environ: Mapping[str, str] | None = None) -> list[str]:
    """The installed modules' ``@mcp_tool`` registry: typed schemas, and ``after="20"`` coerced to 20."""

    (workspace / "sample.py").write_text(SAMPLE, encoding="utf-8")
    env = {**(os.environ if environ is None else environ), "CLAUDE_WORKSPACE_ROOT": str(workspace)}
    done = subprocess.run(
        [python, "-I", "-c", REGISTRY_PROBE, str(workspace)],
        capture_output=True,
        text=True,
        cwd=workspace,
        env=env,
        timeout=600,
        check=False,
    )
    if done.returncode != 0:
        return [f"the registry probe failed (exit {done.returncode}): {done.stderr[-2000:]}"]
    found = json.loads(done.stdout.strip().splitlines()[-1])
    print(f"registry probe: {len(found['tools'])} tools; framework {Path(found['framework']).name}")
    problems = _schema_problems("registry", found["tools"])
    after = found["tools"].get("grep", {}).get("after") or {}
    if after.get("type") != "integer":
        problems.append(f"registry grep.after is not an integer: {after}")
    problems += _coercion_problems("registry grep(after='20')", found["typed"], found["coerced"], found["none"])
    return problems


def check_served(lc: str, workspace: Path) -> list[str]:
    """What ``lc runtime mcp`` serves: typed schemas, and ``bash(timeout="20")`` answered like ``20``."""

    def bash(rid: int, timeout: Any) -> dict[str, Any]:
        arguments = {"command": "echo release-slice", "timeout": timeout}
        return _rpc(rid, "tools/call", {"name": "bash", "arguments": arguments})

    # Runtime Lite serves a Git worktree only.
    if not (workspace / ".git").exists():
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    try:
        responses = mcp_session(lc, workspace, [_rpc(2, "tools/list", {}), bash(3, 20), bash(4, "20")])
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return [f"served: {str(exc)[:2000]}"]
    listed = (responses.get(2, {}).get("result") or {}).get("tools") or ()
    tools = {tool["name"]: (tool.get("inputSchema") or {}).get("properties") or {} for tool in listed}
    if not tools:
        return [f"tools/list returned no tools: {responses.get(2)}"]
    problems = _schema_problems("served", tools)
    if (tools.get("bash", {}).get("timeout") or {}).get("type") != "integer":
        problems.append(f"served bash.timeout is not an integer: {tools.get('bash', {}).get('timeout')}")
    missing = {"error": "no response"}
    problems += _coercion_problems(
        "served bash(timeout='20')", responses.get(3) or missing, responses.get(4) or missing, {}
    )
    return problems


def _review(lc: str, url: str, repo: Path) -> dict[str, Any]:
    done = subprocess.run(
        [lc, "review", "--repo-root", str(repo), "--track", "--no-open", "--no-impact", "--no-provenance", "--json"],
        capture_output=True,
        text=True,
        env={**os.environ, "LEMONCROW_URL": url},
        cwd=repo,
        timeout=600,
        check=False,
    )
    if done.returncode != 0:
        raise AssertionError(f"lc review exited {done.returncode}: {done.stderr[-1500:]}")
    json.loads(done.stdout)  # the Review packet
    # The tracked Review's identity (review_id r/..., revision_id rr/...) is one JSON line on stderr.
    for line in reversed(done.stderr.splitlines()):
        if line.startswith("{") and '"review_id"' in line:
            found: dict[str, Any] = json.loads(line)
            return found
    raise AssertionError(f"lc review --track reported no Review identity: {done.stderr[-1500:]}")


def check_review(lc: str, url: str, repo: Path) -> list[str]:
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=10) as response:
            if response.status != 200:
                return [f"{url}/healthz answered {response.status}"]
    except OSError as exc:
        return [f"the candidate's Review server did not answer {url}/healthz: {exc}"]
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-q"),
        ("config", "user.email", "release-slice@example.invalid"),
        ("config", "user.name", "LemonCrow release slice"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    try:
        (repo / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
        first = _review(lc, url, repo)
        (repo / "app.py").write_text("def answer():\n    return 43\n", encoding="utf-8")
        second = _review(lc, url, repo)
    except (AssertionError, ValueError) as exc:
        return [str(exc)[:1500]]
    if not first.get("review_id") or first.get("review_id") != second.get("review_id"):
        return [f"the rerun did not update the same Review: {first.get('review_id')} -> {second.get('review_id')}"]
    if not first.get("revision_id") or first.get("revision_id") == second.get("revision_id"):
        return [f"the changed rerun made no new Revision: {first.get('revision_id')} -> {second.get('revision_id')}"]
    print(f"review: {first['review_id']} {first['revision_id']} -> {second['revision_id']}")
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lc", required=True, help="the installed candidate's lc")
    parser.add_argument("--python", required=True, help="the installed candidate's interpreter")
    parser.add_argument("--url", required=True, help="the candidate's local Review server")
    parser.add_argument("--work", required=True, type=Path, help="a scratch directory")
    args = parser.parse_args(argv)
    workspace = args.work / "mcp-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    problems = check_registry(args.python, workspace) + check_served(args.lc, workspace)
    problems += [f"review: {item}" for item in check_review(args.lc, args.url, args.work / "review-repo")]
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if problems:
        return 1
    print("compiled candidate slice: typed MCP schemas, '20' -> 20 coercion, Review routes: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
