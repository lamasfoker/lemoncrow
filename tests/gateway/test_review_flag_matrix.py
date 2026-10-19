"""Feature-flag state matrix through the Review API (Phase 9R item 44, ``behavior.flag_matrix``).

Real sealed sandbox runs (bubblewrap): a trusted-base command runs on base and
head once per touched-flag state; the bound is tested at and past its limit.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.gateway.test_review_artifact_analyses_api import _analyses, _behavior, _ok, _plan_entry, _record, _run
from tests.gateway.test_review_check_runs_api import _review
from tests.gateway.test_review_hermetic import needs_bwrap

ENTRY = "behavior.flag_matrix"
FLAGS = (
    "analyses:\n"
    "  flags:\n"
    "    - id: checks\n"
    "      command: [python3, check.py]\n"
    "      env: FEATURE_{FLAG}\n"
    "      junit: report.xml\n"
)
THOROUGH = "review:\n  effort: thorough\n" + FLAGS
DEEP = "review:\n  effort: deep\n" + FLAGS
FLAGS_PY = (
    "import os\n\n"
    "def is_enabled(name):\n"
    "    return os.environ.get('FEATURE_' + name.upper().replace('-', '_')) == '1'\n"
)
CHECK = (
    "import os, sys\n"
    "from app import checkout_total\n"
    "from banner import banner\n"
    "cases = [\n"
    "    ('test_total', checkout_total(10) == 10),\n"
    "    ('test_banner', banner() == 'hello'),\n"
    # An untouched flag never reaches the command: it stays at its default.
    "    ('test_untouched_default', 'FEATURE_LEGACY_MODE' not in os.environ),\n"
    "]\n"
    "xml = '<testsuite>' + ''.join('<testcase classname=\"check\" name=\"%s\">%s</testcase>'\n"
    "    % (name, '' if ok else '<failure/>') for name, ok in cases) + '</testsuite>'\n"
    "open('report.xml', 'w').write(xml)\n"
    "sys.exit(0 if all(ok for _, ok in cases) else 1)\n"
)
BASE = {
    "flags.py": FLAGS_PY,
    "check.py": CHECK,
    "banner.py": "from flags import is_enabled\n\ndef banner():\n    return 'legacy' if is_enabled('legacy-mode') else 'hello'\n",
    "app.py": "from flags import is_enabled\n\ndef checkout_total(x):\n    return x\n",
}


def _app(*flags: str, broken: str = "") -> str:
    """An app module reading *flags*; *broken* doubles the total when that flag is on."""

    lines = ["from flags import is_enabled", "", "def checkout_total(x):"]
    for flag in flags:
        effect = "x * 2" if flag == broken else "x"
        lines.append(f"    if is_enabled('{flag}'):\n        return {effect}")
    lines.append("    return x")
    return "\n".join(lines) + "\n"


def _matrix(review: SimpleNamespace) -> dict[str, Any]:
    return _record(review, ENTRY, "checks")


@needs_bwrap
def test_a_new_flag_runs_on_off_diffs_and_untouched_flags_stay_at_default(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        base={".lemoncrow/review.yaml": THOROUGH, **BASE},
        head={"app.py": _app("new-checkout", broken="new-checkout")},
    )
    assert [row["id"] for row in _analyses(review)["entries"]] == [ENTRY]
    report = _ok(review, ENTRY)
    assert report["verdict"] == "FAIL" and report["executions"] == 2  # one sealed run per side
    record = _matrix(review)
    summary = record["summary"]
    assert [(row["flag"], row["touch"], row["env"]) for row in summary["flags"]] == [
        ("new-checkout", "introduced", "FEATURE_NEW_CHECKOUT")
    ]
    rows = {row["state"]: row for row in summary["matrix"]}
    assert set(rows) == {"new-checkout=on", "new-checkout=off"}
    assert rows["new-checkout=on"]["status"] == "regressed" and rows["new-checkout=on"]["tests_changed"] == 1
    assert rows["new-checkout=on"]["env"] == {"FEATURE_NEW_CHECKOUT": "1"}
    assert rows["new-checkout=off"]["status"] == "same"
    assert rows["new-checkout=off"]["env"] == {"FEATURE_NEW_CHECKOUT": "0"}
    # legacy-mode is read only by an unchanged file: never part of the matrix, never set.
    assert "legacy-mode" not in str(summary["matrix"])
    findings = {row["subject"]: row for row in record["findings"]}
    assert set(findings) == {"checks::new-checkout=on::check::test_total"}
    assert findings["checks::new-checkout=on::check::test_total"]["critical"] is True
    targets = [row for row in _behavior(review) if row["kind"] == "test_outcome" and "new-checkout" in row["title"]]
    assert len(targets) == 1 and targets[0]["critical"] is True
    assert summary["bound"] == {"depth": "thorough", "max_flags": 2, "touched": 1, "limited_flags": 0}


@needs_bwrap
def test_the_matrix_bound_is_explicit_at_and_past_the_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for case_index, (config, flags, ran, limited) in enumerate(
        [
            (THOROUGH, ("a-flag", "b-flag"), 2, 0),
            (THOROUGH, ("a-flag", "b-flag", "c-flag"), 2, 1),
            (DEEP, ("a-flag", "b-flag", "c-flag", "d-flag"), 4, 0),
            (DEEP, ("a-flag", "b-flag", "c-flag", "d-flag", "e-flag"), 4, 1),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        with monkeypatch.context() as case_monkeypatch:
            from lemoncrow.pro.capabilities.review import flag_matrix

            executed: list[str] = []
            inspect = flag_matrix.inspect

            def spy(
                report: dict[str, Any], exit_code: int | None, _executed: list[str] = executed, _inspect: Any = inspect
            ) -> tuple[dict[str, Any] | None, str]:
                _executed.extend(str(row.get("id")) for row in report.get("states") or ())
                return _inspect(report, exit_code)

            case_monkeypatch.setattr(flag_matrix, "inspect", spy)
            review = _review(
                case_tmp_path, base={".lemoncrow/review.yaml": config, **BASE}, head={"app.py": _app(*flags)}
            )
            report = _ok(review, ENTRY)
            assert sorted(executed) == sorted(
                [f"{flag}={value}" for flag in flags[:ran] for value in ("on", "off")] * 2
            )
            summary = _matrix(review)["summary"]
            by_flag: dict[str, set[str]] = {}
            for row in summary["matrix"]:
                by_flag.setdefault(row["flag"], set()).add(row["status"])
            assert len(by_flag) == len(flags)
            assert sorted((flag for flag, states in by_flag.items() if states == {"same"})) == sorted(flags[:ran])
            limited_flags = sorted((flag for flag, states in by_flag.items() if states == {"limited_by_budget"}))
            assert limited_flags == sorted(flags[ran:])
            assert summary["bound"]["limited_flags"] == limited
            for row in summary["matrix"]:
                if row["status"] == "limited_by_budget":
                    assert row["reason"].startswith("Limited by budget") and "at most" in row["reason"]
            assert report["verdict"] == ("NOT_RUN" if limited else "PASS")


def test_no_touched_flag_is_an_empty_matrix_that_runs_nothing(tmp_path: Path) -> None:
    review = _review(tmp_path, base={".lemoncrow/review.yaml": THOROUGH, **BASE}, head={"README.md": "docs\n"})
    report = _ok(review, ENTRY)
    assert report["verdict"] == "PASS" and report["executions"] == 0
    assert _matrix(review)["summary"]["matrix"] == []


def test_an_exhausted_budget_limits_every_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import artifact_analysis_api

    monkeypatch.setattr(artifact_analysis_api, "analysis_budget_seconds", lambda _plan, _entry: 0.0)
    review = _review(tmp_path, base={".lemoncrow/review.yaml": THOROUGH, **BASE}, head={"app.py": _app("x-flag")})
    report = _ok(review, ENTRY)
    assert report["verdict"] == "NOT_RUN" and report["executions"] == 0
    matrix = _matrix(review)["summary"]["matrix"]
    assert {row["status"] for row in matrix} == {"limited_by_budget"} and len(matrix) == 2


@pytest.mark.parametrize(
    ("effort", "admitted"), [("auto", False), ("thorough", True), ("deep", True)], ids=["auto", "thorough", "deep"]
)
def test_the_matrix_is_thorough_or_deep_work(tmp_path: Path, effort: str, admitted: bool) -> None:
    config = f"review:\n  effort: {effort}\n" + FLAGS
    review = _review(tmp_path, base={".lemoncrow/review.yaml": config, **BASE}, head={"README.md": "docs\n"})
    entry = _plan_entry(review, ENTRY)
    response = _run(review, ENTRY)
    if admitted:
        assert entry is not None and entry["status"] == "run", entry
        assert response.status_code == 200, response.text
    else:
        assert entry is None or entry["status"] != "run", entry
        assert response.status_code == 409, response.text


def test_without_a_flag_command_the_matrix_is_inapplicable(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        base={".lemoncrow/review.yaml": "review:\n  effort: thorough\n", **BASE},
        head={"app.py": _app("new-checkout")},
    )
    entry = _plan_entry(review, ENTRY)
    assert entry is not None and entry["status"] == "inapplicable", entry
    assert "analyses.flags" in entry["reason"]
    assert _run(review, ENTRY).status_code == 409


def test_flag_configuration_is_validated(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.flag_matrix import env_name, parse_specs

    errors: list[str] = []
    specs = parse_specs(
        [
            {"id": "ok", "command": "python3 check.py", "env": "FF_{flag}", "on": "true", "off": "false"},
            {"id": "path", "command": "x", "env": "PATH{FLAG}"},
            {"id": "ld", "command": "x", "env": "LD_{FLAG}"},
            {"id": "same", "command": "x", "on": "1", "off": "1"},
            {"id": "none", "command": []},
        ],
        errors,
    )
    assert [spec.id for spec in specs] == ["ok", "path"]
    assert len(errors) == 3
    assert env_name("FF_{flag}", "new.checkout-v2") == "FF_new_checkout_v2"
    assert env_name("FEATURE_{FLAG}", "new-checkout") == "FEATURE_NEW_CHECKOUT"
