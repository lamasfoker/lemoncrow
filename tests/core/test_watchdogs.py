from __future__ import annotations

from lemoncrow.core.foundation.models import Playbook
from lemoncrow.pro.foundation.watchdogs import (
    ContextBloat,
    HighRiskAction,
    KnownDeadEnd,
    RepeatedCommandFailure,
    RepeatedToolCall,
    SessionState,
    SkippedVerification,
    args_signature,
    error_signature,
    run_watchdogs,
)


def _block(dead: tuple[str, ...] | list[str] = ()) -> Playbook:
    return Playbook(
        id="b",
        title="T",
        domain="coding",
        situation="s",
        procedure=["x"],
        dead_ends=list(dead),
    )


def test_run_watchdogs_returns_alerts_list() -> None:
    state = SessionState(
        command_results=[("ls", False, "sig"), ("ls", False, "sig")],
        tool_calls=[("shopify.publish", "x")],
    )
    alerts = run_watchdogs(state, [])
    names = {a.watchdog for a in alerts}
    assert "repeated_command_failure" in names
    assert "high_risk_action" in names


def test_watchdog_signature_contract() -> None:
    assert error_signature("Failed at 0xABCD123, retry 17") == error_signature("Failed at 0x9999111, retry 42")
    assert args_signature({"a": 1, "b": 2}) == args_signature({"b": 2, "a": 1})


def test_individual_watchdog_trigger_contract() -> None:
    repeated_command = RepeatedCommandFailure().check(SessionState(command_results=[("ls", False, "sigA")] * 2), [])
    assert repeated_command is not None and repeated_command.severity == "high"
    assert RepeatedToolCall().check(SessionState(tool_calls=[("t", "s")] * 3), []) is not None
    dead_end = KnownDeadEnd().check(
        SessionState(plan=["Parse the product handle from url"]), [_block(dead=["product handle from url"])]
    )
    assert dead_end is not None and dead_end.severity == "high"
    assert SkippedVerification().check(SessionState(declared_success=True, validation_passed=False), []) is not None
    assert ContextBloat().check(SessionState(tool_outputs_chars=60_000), []) is not None
    state = SessionState(tool_calls=[("shopify.publish", "sig")], rubric_run=False)
    assert HighRiskAction().check(state, []) is not None
    state.rubric_run = True
    assert HighRiskAction().check(state, []) is None
