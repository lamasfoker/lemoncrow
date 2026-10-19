"""Real isolated agent process gate (not a live model/provider qualification)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from lemoncrow_client.review_agent_adapters import AgentInvocationRefused, LocalAgentInvocation
from lemoncrow_client.review_agent_sandbox import AgentSandbox, AgentSandboxLimits


@pytest.fixture
def fixture(tmp_path: Path) -> tuple[AgentSandbox, LocalAgentInvocation, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    source = tmp_path / "snapshot"
    source.mkdir()
    (source / "file.py").write_text("value = 1\n", encoding="utf-8")
    agent = tmp_path / "provider"
    agent.write_text(
        "#!/usr/bin/python3\nimport os, pathlib, socket, sys\n"
        "print('snap=' + pathlib.Path('file.py').read_text().strip())\n"
        "print('home=' + str(os.getenv('HOME')))\n"
        "print('secret=' + str(os.getenv('AGENT_TEST_SECRET')))\n"
        "try: pathlib.Path('file.py').write_text('evil')\n"
        "except OSError: print('readonly=true')\n"
        "else: sys.exit(3)\n"
        "try: socket.socket().connect(('1.1.1.1', 80))\n"
        "except OSError: print('no_network=true')\n"
        "else: sys.exit(4)\n",
        encoding="utf-8",
    )
    agent.chmod(0o700)
    invocation = LocalAgentInvocation(
        provider="codex",
        executable=str(agent),
        argv=(str(agent),),
        stdin="one local approved prompt",
        cwd=repo,
        environment_overrides={},
        task_id="task",
        review_id="review",
        snapshot_digest="a" * 64,
        max_runtime_seconds=10,
    )
    return AgentSandbox(), invocation, source


def test_real_bubblewrap_probes_isolation(fixture: tuple[AgentSandbox, LocalAgentInvocation, Path]) -> None:
    sandbox, _, _ = fixture
    ok, reason = sandbox.qualify()
    assert ok, reason


def test_source_read_only_no_ambient_secrets_no_network(
    fixture: tuple[AgentSandbox, LocalAgentInvocation, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox, invocation, source = fixture
    monkeypatch.setenv("AGENT_TEST_SECRET", "private-should-not-inherit")
    result = sandbox.run(invocation, snapshot=source, limits=AgentSandboxLimits())
    assert result.status == "exited", result.output
    assert "snap=value = 1" in result.output
    assert "home=/nonexistent" in result.output
    assert "secret=None" in result.output
    assert "readonly=true" in result.output
    assert "no_network=true" in result.output
    assert (source / "file.py").read_text() == "value = 1\n"
    assert result.provenance == "author_agent_unverified"
    assert result.model_usage_qualified is False


def test_no_sandbox_never_runs(fixture: tuple[AgentSandbox, LocalAgentInvocation, Path]) -> None:
    _, invocation, source = fixture
    with pytest.raises(AgentInvocationRefused, match="Bubblewrap"):
        AgentSandbox(bubblewrap=Path("/nonexistent/no-bwrap")).run(
            invocation, snapshot=source, limits=AgentSandboxLimits()
        )


def test_refuses_live_mutable_checkout(fixture: tuple[AgentSandbox, LocalAgentInvocation, Path]) -> None:
    sandbox, invocation, _ = fixture
    with pytest.raises(AgentInvocationRefused, match="separately materialized"):
        sandbox.run(invocation, snapshot=invocation.cwd, limits=AgentSandboxLimits())


def test_kills_long_running_process_without_leaving_side_effects(
    fixture: tuple[AgentSandbox, LocalAgentInvocation, Path], tmp_path: Path
) -> None:
    sandbox, invocation, source = fixture
    child = tmp_path / "slow"
    child.write_text(
        "#!/usr/bin/python3\nimport time\nprint('running', flush=True)\ntime.sleep(20)\n", encoding="utf-8"
    )
    child.chmod(0o700)
    slow = LocalAgentInvocation(
        provider="codex",
        executable=str(child),
        argv=(str(child),),
        stdin="check",
        cwd=invocation.cwd,
        environment_overrides={},
        task_id="t",
        review_id="r",
        snapshot_digest="a" * 64,
        max_runtime_seconds=10,
    )
    began = time.monotonic()
    result = sandbox.run(
        slow,
        snapshot=source,
        limits=AgentSandboxLimits(wall_seconds=5),
        should_cancel=lambda: time.monotonic() - began > 0.5,
        cancellation_poll_s=0.1,
    )
    assert result.status == "cancelled"
    assert time.monotonic() - began < 5
    assert result.model_usage_qualified is False


def test_deadline_bounded_even_when_agent_ignores_shutdown(
    fixture: tuple[AgentSandbox, LocalAgentInvocation, Path], tmp_path: Path
) -> None:
    sandbox, invocation, source = fixture
    child = tmp_path / "hung"
    child.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(15)\n", encoding="utf-8")
    child.chmod(0o700)
    hung = LocalAgentInvocation(
        provider="codex",
        executable=str(child),
        argv=(str(child),),
        stdin="check",
        cwd=invocation.cwd,
        environment_overrides={},
        task_id="t",
        review_id="r",
        snapshot_digest="a" * 64,
        max_runtime_seconds=5,
    )
    result = sandbox.run(hung, snapshot=source, limits=AgentSandboxLimits(wall_seconds=1), cancellation_poll_s=0.1)
    assert result.status == "timed_out"


def test_output_is_bounded(fixture: tuple[AgentSandbox, LocalAgentInvocation, Path], tmp_path: Path) -> None:
    sandbox, invocation, source = fixture
    child = tmp_path / "noisy"
    child.write_text("#!/usr/bin/python3\nprint('X' * 300000)\n", encoding="utf-8")
    child.chmod(0o700)
    noisy = LocalAgentInvocation(
        provider="codex",
        executable=str(child),
        argv=(str(child),),
        stdin="check",
        cwd=invocation.cwd,
        environment_overrides={},
        task_id="t",
        review_id="r",
        snapshot_digest="a" * 64,
        max_runtime_seconds=5,
    )
    result = sandbox.run(noisy, snapshot=source, limits=AgentSandboxLimits(wall_seconds=3))
    assert len(result.output.encode("utf-8")) <= 65536
    assert result.status != "exited"


def test_typed_limits_and_before_start_cancellation(fixture: tuple[AgentSandbox, LocalAgentInvocation, Path]) -> None:
    sandbox, invocation, source = fixture
    for kwargs in ({"wall_seconds": 301}, {"memory_mb": 1}, {"process_count": True}):
        with pytest.raises(ValueError):
            AgentSandboxLimits(**kwargs)
    assert (
        sandbox.run(invocation, snapshot=source, limits=AgentSandboxLimits(), should_cancel=lambda: True).status
        == "cancelled"
    )


def test_rejects_agent_binary_supplied_by_untrusted_source(
    fixture: tuple[AgentSandbox, LocalAgentInvocation, Path],
) -> None:
    sandbox, invocation, source = fixture
    checkout_agent = invocation.cwd / "fake-agent"
    checkout_agent.write_text("#!/bin/sh\necho compromised\n", encoding="utf-8")
    checkout_agent.chmod(0o700)
    unsafe = LocalAgentInvocation(
        provider="codex",
        executable=str(checkout_agent),
        argv=(str(checkout_agent),),
        stdin="prompt",
        cwd=invocation.cwd,
        environment_overrides={},
        task_id="t",
        review_id="r",
        snapshot_digest="a" * 64,
        max_runtime_seconds=5,
    )
    with pytest.raises(AgentInvocationRefused, match="untrusted repository"):
        sandbox.run(unsafe, snapshot=source, limits=AgentSandboxLimits())
