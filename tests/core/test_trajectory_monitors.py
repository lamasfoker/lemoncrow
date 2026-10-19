"""Tests for the six trajectory monitors."""

from __future__ import annotations

from lemoncrow.pro.capabilities.monitors.suite import (
    DEFAULT_WEIGHTS,
    MonitorResult,
    _claim_contradiction,
    _cyclic_compression,
    _late_sprawl,
    _semantic_loop,
    _silent_topic_drift,
    _verification_skip,
    evaluate_all,
)


def test_semantic_loop_contract() -> None:
    """case: test_semantic_loop_no_repetition"""
    steps = [
        "I read the file and found a bug in line 42.",
        "I searched for similar patterns and found none.",
        "I edited the function to fix the null check.",
        "I ran the tests and they passed.",
    ]
    score = _semantic_loop(steps)
    assert score < 0.5, f"Expected low loop score, got {score}"
    "case: test_semantic_loop_detects_repetition"
    repeated = "The file src/main.py contains the handler function with error handling."
    steps = [
        repeated,
        "I looked at the imports.",
        repeated + " There is also a bug.",
        repeated + " Same handler, same file.",
    ]
    score = _semantic_loop(steps)
    assert score >= 0.5, f"Expected high loop score, got {score}"
    "case: test_semantic_loop_empty_steps"
    assert _semantic_loop([]) == 0.0
    assert _semantic_loop(["only one"]) == 0.0
    "case: test_semantic_loop_distinct_short_lines_not_a_loop"
    assert _semantic_loop(["ok.", "yes."]) == 0.0


def test_verification_and_contradiction_contract() -> None:
    """case: test_verification_skip_with_verification"""
    steps = [
        "I made the change to the handler.",
        "I ran the tests to verify the fix works.",
        "The tests passed — the fix is complete.",
    ]
    assert _verification_skip(steps) == 0.0
    "case: test_verification_skip_without_verification"
    steps = ["I looked at the code.", "I think the solution is to add a null check.", "The fix is done and complete."]
    score = _verification_skip(steps)
    assert score >= 0.5, f"Expected high skip score, got {score}"
    "case: test_claim_contradiction_no_contradiction"
    steps = ["The function exists in utils.py.", "I can read the file.", "The implementation looks correct."]
    assert _claim_contradiction(steps) < 0.3
    "case: test_claim_contradiction_detects_reversal"
    steps = [
        "It works correctly and there is no error.",
        "I ran the tests.",
        "There was an error and the tests failed.",
    ]
    score = _claim_contradiction(steps)
    assert score >= 0.3, f"Expected contradiction score, got {score}"


def test_cycle_and_sprawl_contract() -> None:
    """case: test_cyclic_compression_no_cycle"""
    steps = [
        "I read the config file.",
        "I found the database URL.",
        "I updated the connection string.",
        "I ran migrations.",
        "I tested the connection.",
        "Everything works.",
    ]
    score = _cyclic_compression(steps)
    assert score < 0.6
    "case: test_cyclic_compression_detects_cycle"
    step = "The handler function in src/api/handler.py processes the request and returns a response."
    steps = [step, "Did some work.", "More work.", "Even more work.", "Still working.", step]
    score = _cyclic_compression(steps)
    assert score >= 0.5, f"Expected cycle score, got {score}"
    "case: test_late_sprawl_no_sprawl"
    steps = ["Fix null check.", "Edit handler.", "Run tests.", "Tests pass."]
    assert _late_sprawl(steps) == 0.0
    "case: test_late_sprawl_detects_scope_creep"
    steps = [
        "I'm fixing the null check.",
        "Edit the handler.",
        "I noticed the tests also need refactoring.",
        "Additionally I should clean up the imports.",
        "While I'm at it I'll also improve the error messages.",
    ]
    score = _late_sprawl(steps)
    assert score > 0.0, f"Expected sprawl score, got {score}"


def test_topic_drift_contract() -> None:
    """case: test_silent_topic_drift_no_drift"""
    task = "Fix the authentication bug in login handler"
    steps = [
        "I'm looking at the authentication login handler.",
        "The bug is in the authentication check.",
        "I fixed the authentication handler for login.",
    ]
    score = _silent_topic_drift(steps, task)
    assert score < 0.5, f"Expected low drift score, got {score}"
    "case: test_silent_topic_drift_detects_drift"
    task = "Fix the authentication bug in login handler"
    steps = [
        "I'm looking at the database schema.",
        "The migration files need updating.",
        "I'm reviewing the CSS styles now.",
    ]
    score = _silent_topic_drift(steps, task)
    assert score >= 0.5, f"Expected high drift score, got {score}"
    "case: test_silent_topic_drift_no_task"
    assert _silent_topic_drift(["anything"], "") == 0.0


def test_monitor_suite_contract() -> None:
    """case: test_evaluate_all_healthy_run"""
    task = "Fix the null pointer exception in the request handler"
    steps = [
        "I read src/handler.py and found the null pointer exception on line 42.",
        "The issue is that request.user can be None before the permission check.",
        "I added a null guard: if request.user is None: return 401.",
        "I ran the tests with pytest and all 23 tests passed.",
        "The fix is complete and verified.",
    ]
    result = evaluate_all(steps, task=task)
    assert isinstance(result, MonitorResult)
    assert result.composite < 0.4, f"Healthy run should have low composite, got {result.composite}"
    "case: test_evaluate_all_looping_run"
    task = "Fix the authentication bug"
    repeated = "The authentication handler in auth.py contains the login function."
    steps = [
        repeated,
        repeated + " I need to look at this.",
        repeated + " There is a bug somewhere.",
        "I think the solution is done.",
        repeated + " Same issue persists.",
    ]
    result = evaluate_all(steps, task=task)
    assert result.composite > 0.1, f"Looping run should have elevated composite, got {result.composite}"
    "case: test_evaluate_all_returns_fired_monitors"
    steps = [
        "It works correctly with no errors.",
        "I finished the implementation.",
        "The solution is complete and done.",
    ]
    result = evaluate_all(steps, task="fix the bug")
    assert isinstance(result.fired, list)
    assert "verification_skip" in result.fired
    "case: test_evaluate_all_custom_weights"
    steps = ["step1", "step2", "step1 again very similar"]
    result = evaluate_all(steps, weights={"semantic_loop": 1.0})
    assert result.scores["semantic_loop"] >= 0.0
    "case: test_evaluate_all_failure_type_is_highest_weight_fired"
    "failure_type should be the highest-weight fired monitor."
    steps = ["It works correctly.", "It works correctly.", "The fix is done.", "There was an error."]
    result = evaluate_all(steps)
    if result.failure_type is not None:
        assert result.failure_type in result.fired
        fired_weights = {n: DEFAULT_WEIGHTS.get(n, 0.0) for n in result.fired}
        assert DEFAULT_WEIGHTS.get(result.failure_type, 0.0) == max(fired_weights.values())
    "case: test_evaluate_all_empty_steps"
    result = evaluate_all([], task="anything")
    assert result.composite == 0.0
    assert result.fired == []
    assert result.failure_type is None
