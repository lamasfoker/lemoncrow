from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.core.foundation.runtime_trace import TraceStore
from lemoncrow.gateway.cli.app import cli
from lemoncrow.gateway.cli.developer_tools import developer_tools
from tests.core.test_trace_replay import _records


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    store = TraceStore(tmp_path)
    for record in _records():
        store.append(record)
    return tmp_path


def _run(root: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, ["--root", str(root), *args], catch_exceptions=False)
    return result.exit_code, result.output


def _run_dev(root: Path, *args: str) -> tuple[int, str]:
    # `lc dev` execs the checkout's tools (b222aebc5); invoke their entry point in-process.
    result = CliRunner().invoke(developer_tools, ["--root", str(root), *args], catch_exceptions=False)
    return result.exit_code, result.output


def test_trace_show_list_replay_compare(root: Path) -> None:
    code, out = _run(root, "runtime", "trace", "show", "lt_0000000000000001")
    assert code == 0 and "layers    client, server" in out and "→ src/util.py" in out

    code, out = _run(root, "runtime", "trace", "list", "--json")
    assert code == 0 and next(r["trace_id"] for r in json.loads(out)) == "lt_0000000000000003"

    code, out = _run(root, "runtime", "trace", "replay", "lt_0000000000000001", "--ranker", "feature-v1", "--json")
    payload = json.loads(out)
    assert code == 0 and payload["replay"]["selected"][0] == "src/review/auth.py"

    code, out = _run(root, "runtime", "trace", "compare", "lt_0000000000000001", "lt_0000000000000002")
    assert code == 0 and json.loads(out)["tool"] == ["code_search", "read"]


def test_trace_show_unknown_id_fails(root: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(root), "runtime", "trace", "show", "lt_ffffffffffffffff"])
    assert result.exit_code != 0 and "trace not found" in result.output


def test_trace_list_includes_runtime_lite_and_client_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clients write under ``<LEMONCROW_HOME>/runtime``; the reader must look there too."""
    home = tmp_path / "home"
    monkeypatch.setenv("LEMONCROW_HOME", str(home))
    TraceStore(home / "runtime").append(
        {"schema_version": 1, "trace_id": "lt_00000000000000aa", "writer": "client", "tool": "code_search"}
    )

    code, out = _run(tmp_path / "store", "runtime", "trace", "list", "--json")
    assert code == 0
    assert [row["trace_id"] for row in json.loads(out)] == ["lt_00000000000000aa"]


def test_eval_replay_reports_and_gates_on_evidence(root: Path) -> None:
    code, out = _run_dev(root, "eval", "replay", "--candidate-ranker", "feature-v1", "--json")
    payload = json.loads(out)
    assert payload["cases_labeled"] == 1 and payload["candidate"]["mrr"] == 1.0
    assert payload["verdict"] == "insufficient_evidence" and code == 1
    code, out = _run_dev(root, "eval", "replay", "--candidate-ranker", "feature-v1", "--min-labeled", "1", "--json")
    assert json.loads(out)["verdict"] == "no_regression" and code == 0
