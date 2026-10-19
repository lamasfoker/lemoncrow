"""The whole-suite guard refuses broad runs unless a person opts in for one command."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import (
    FOCUSED_TEST_LIMIT,
    FULL_SUITE_OPT_IN,
    pytest_collection_finish,
    pytest_xdist_node_collection_finished,
)


def _session(count: int, *, collectonly: bool = False, worker: bool = False) -> pytest.Session:
    config = SimpleNamespace(option=SimpleNamespace(collectonly=collectonly))
    if worker:
        config.workerinput = {}
    return SimpleNamespace(items=[object()] * count, config=config)  # type: ignore[return-value]


def test_xdist_controller_refuses_what_its_workers_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FULL_SUITE_OPT_IN, raising=False)
    worker = _session(10_000, worker=True)
    pytest_collection_finish(worker)
    with pytest.raises(pytest.exit.Exception):
        pytest_xdist_node_collection_finished(worker, ["t"] * 10_000)


def test_runs_at_the_limit_and_refuses_one_past_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FULL_SUITE_OPT_IN, raising=False)
    pytest_collection_finish(_session(FOCUSED_TEST_LIMIT))
    with pytest.raises(pytest.exit.Exception) as refused:
        pytest_collection_finish(_session(FOCUSED_TEST_LIMIT + 1))
    assert refused.value.returncode == pytest.ExitCode.USAGE_ERROR
    assert FULL_SUITE_OPT_IN in refused.value.msg


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_only_the_exact_opt_in_admits_the_whole_suite(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(FULL_SUITE_OPT_IN, value)
    with pytest.raises(pytest.exit.Exception):
        pytest_collection_finish(_session(10_000))
    monkeypatch.setenv(FULL_SUITE_OPT_IN, "1")
    pytest_collection_finish(_session(10_000))


def test_collection_only_is_never_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FULL_SUITE_OPT_IN, raising=False)
    pytest_collection_finish(_session(10_000, collectonly=True))
