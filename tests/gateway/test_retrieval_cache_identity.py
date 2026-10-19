import json
from types import SimpleNamespace

import pytest

from lemoncrow.gateway.cli.retrieval_cache import cache_identity, cached_result, source_fingerprint


def test_source_fingerprint_includes_dirty_and_override_sources(tmp_path):
    local = tmp_path / "src/lemoncrow/code.py"
    local.parent.mkdir(parents=True)
    local.write_text("one")
    override = tmp_path / "snapshot/lemoncrow/code.py"
    override.parent.mkdir(parents=True)
    override.write_text("old")
    env = {"PYTHONPATH": str(tmp_path / "snapshot")}
    first = source_fingerprint(tmp_path, env)
    local.write_text("two")
    second = source_fingerprint(tmp_path, env)
    assert first != second
    override.write_text("new")
    assert source_fingerprint(tmp_path, env) != second


def test_source_fingerprint_covers_every_first_party_override_distribution(tmp_path):
    addon = tmp_path / "overlay" / "lemoncrow_addon" / "provider.py"
    addon.parent.mkdir(parents=True)
    addon.write_text("old")
    unrelated = tmp_path / "overlay" / "vendored" / "lib.py"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("old")
    env = {"PYTHONPATH": str(tmp_path / "overlay")}
    first = source_fingerprint(tmp_path, env)
    unrelated.write_text("new")
    assert source_fingerprint(tmp_path, env) == first
    addon.write_text("new")
    assert source_fingerprint(tmp_path, env) != first


def test_cache_identity_rejects_different_runs(tmp_path):
    for case_index, change in enumerate(["corpus", "repo", "sample", "runtime", "config"]):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        gold = case_tmp_path / "gold.json"
        gold.write_text("{}")
        command = ["python", "eval.py", "--repo", "django", "--sample", "10"]
        env = {"LEMONCROW_ZOEKT_MODE": "off"}
        source = "runtime-a"
        first = cache_identity(case_tmp_path, command, env, [gold], source=source)
        if change == "corpus":
            gold.write_text('{"new": true}')
        elif change == "repo":
            command[3] = "flask"
        elif change == "sample":
            command[-1] = "20"
        elif change == "runtime":
            source = "runtime-b"
        else:
            env["LEMONCROW_ZOEKT_MODE"] = "installed"
        second = cache_identity(case_tmp_path, command, env, [gold], source=source)
        envelope = {"cache_schema": 1, "identity": first, "result": {"mrr": 0.7}}
        assert cached_result(envelope, first) == {"mrr": 0.7}
        assert cached_result(envelope, second) is None


def test_old_or_malformed_results_are_not_reused():
    for raw in [None, [], {"mrr": 1}, {"cache_schema": 1, "identity": "a", "result": []}]:
        assert cached_result(raw, "a") is None


@pytest.mark.parametrize("channel,expected_calls", [("lexical", 1), ("lemoncrow-shared", 2)])
def test_cli_resume_checks_identity_before_skipping(tmp_path, monkeypatch, channel, expected_calls):
    import subprocess

    from click.testing import CliRunner

    from benchmarks.codebench import provision_repos
    from lemoncrow.gateway.cli.commands.eval import eval_retrieval

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(provision_repos, "ensure_eval_workspaces", lambda golds: None)
    gold = tmp_path / "gold.json"
    gold.write_text("{}")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"overall": {"mrr": 0.7}}).encode())

    monkeypatch.setattr(subprocess, "run", run)
    args = ["--channel", channel, "--pairs", str(gold), "--csv", str(tmp_path / "out.csv"), "--resume", "--json"]
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(eval_retrieval, args)
        assert result.exit_code == 0, result.output
    assert len(calls) == expected_calls
    result = runner.invoke(eval_retrieval, [*args, "--repo", "different"])
    assert result.exit_code == 0, result.output
    assert len(calls) == expected_calls + 1
