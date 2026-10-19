from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.verified_work.classify import (
    build_opportunity,
    classify_command_kind,
    parse_diagnostics,
)
from lemoncrow.core.capabilities.verified_work.policy import evaluate_eligibility
from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings


def _write_ts_package(root: Path, script: str = "tsc --noEmit") -> None:
    (root / "package.json").write_text(
        '{"scripts":{"typecheck":' + __import__("json").dumps(script) + "}}\n",
        encoding="utf-8",
    )


def test_classify_supported_commands() -> None:
    assert classify_command_kind("npm run typecheck") == "typecheck"
    assert classify_command_kind("cd frontend && npm run typecheck") == "typecheck"
    assert classify_command_kind("uv run mypy src") == "typecheck"
    assert classify_command_kind("ruff check src") == "lint"
    assert classify_command_kind("npx eslint src") == "lint"
    assert classify_command_kind("pytest -q") == "unit_test"
    assert classify_command_kind("npm run typecheck && rm -rf build") is None


def test_parse_tsc_and_ruff_diagnostics(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("const x = 1\n")
    tsc = parse_diagnostics(
        "src/a.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.",
        kind="typecheck",
        cwd=tmp_path,
        workspace=tmp_path,
    )
    assert [(d.path, d.line, d.column, d.code) for d in tsc] == [("src/a.ts", 12, 5, "TS2322")]

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x=1\n")
    ruff = parse_diagnostics(
        "pkg/a.py:1:2: E225 Missing whitespace around operator",
        kind="lint",
        cwd=tmp_path,
        workspace=tmp_path,
    )
    assert [(d.path, d.code) for d in ruff] == [("pkg/a.py", "E225")]


def test_build_opportunity_and_policy(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text("export const n: number = 'x'\n")
    _write_ts_package(tmp_path)
    opp = build_opportunity(
        session_id="s1",
        workspace=tmp_path,
        command="npm run typecheck",
        cwd=tmp_path,
        exit_code=2,
        stdout="src/a.ts(1,14): error TS2322: Type 'string' is not assignable to type 'number'.",
        stderr="",
    )
    assert opp is not None
    assert opp.kind == "typecheck"
    assert opp.implicated_paths == ("src/a.ts",)
    decision = evaluate_eligibility(opp, VerifiedWorkSettings(mode="shadow"))
    assert decision.eligible is True
    assert decision.allowed_paths == ("src/a.ts",)


def test_policy_rejects_test_and_manifest(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "x.test.ts").write_text("x\n")
    _write_ts_package(tmp_path)
    opp = build_opportunity(
        session_id="s1",
        workspace=tmp_path,
        command="npm run typecheck",
        cwd=tmp_path,
        exit_code=2,
        stdout="tests/x.test.ts(1,1): error TS2304: Cannot find name 'x'.",
        stderr="",
    )
    assert opp is not None
    decision = evaluate_eligibility(opp, VerifiedWorkSettings(mode="shadow"))
    assert decision.eligible is False
    assert "test_edit_disabled" in decision.reason_codes


def test_cd_prefix_resolves_diagnostics_inside_subdirectory(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend" / "src"
    frontend.mkdir(parents=True)
    (frontend / "a.ts").write_text("const x = 1\n")
    _write_ts_package(tmp_path / "frontend")
    opp = build_opportunity(
        session_id="s1",
        workspace=tmp_path,
        command="cd frontend && npm run typecheck",
        cwd=tmp_path,
        exit_code=2,
        stdout="src/a.ts(1,1): error TS2304: Cannot find name 'x'.",
        stderr="",
    )
    assert opp is not None
    assert opp.implicated_paths == ("frontend/src/a.ts",)


def test_classify_compile_test_syntax_and_config_commands() -> None:
    assert classify_command_kind("go build ./...") == "compile"
    assert classify_command_kind("go test ./...") == "unit_test"
    assert classify_command_kind("cargo check") == "compile"
    assert classify_command_kind("cargo test -q") == "unit_test"
    assert classify_command_kind("uv run pytest -q tests/test_x.py") == "unit_test"
    assert classify_command_kind("npx vitest run src/foo.test.ts") == "unit_test"
    assert classify_command_kind("python -m py_compile src/foo.py") == "syntax"
    assert classify_command_kind("python -m json.tool config.json") == "config"
    assert classify_command_kind("jq empty config.json") == "config"


def test_parse_go_and_rust_diagnostics(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.go").write_text("package main\n", encoding="utf-8")
    (src / "lib.rs").write_text("fn x() {}\n", encoding="utf-8")

    go = parse_diagnostics(
        "src/main.go:4:9: undefined: thing",
        kind="compile",
        cwd=tmp_path,
        workspace=tmp_path,
    )
    assert [(d.path, d.line, d.column, d.code) for d in go] == [("src/main.go", 4, 9, "compile")]

    rust = parse_diagnostics(
        "error[E0308]: mismatched types\n --> src/lib.rs:7:12\n",
        kind="compile",
        cwd=tmp_path,
        workspace=tmp_path,
    )
    assert [(d.path, d.line, d.column, d.code) for d in rust] == [("src/lib.rs", 7, 12, "E0308")]


def test_unit_test_diagnostics_must_point_to_non_test_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()
    (src / "calc.py").write_text("def f(): pass\n", encoding="utf-8")
    (tests / "test_calc.py").write_text("def test_f(): pass\n", encoding="utf-8")

    source_opp = build_opportunity(
        session_id="s1",
        workspace=tmp_path,
        command="uv run pytest -q",
        cwd=tmp_path,
        exit_code=1,
        stdout="src/calc.py:12: ValueError: bad value",
        stderr="",
    )
    assert source_opp is not None
    source_decision = evaluate_eligibility(
        source_opp,
        VerifiedWorkSettings(mode="shadow", kinds=("unit_test",)),
    )
    assert source_decision.eligible is True

    test_opp = build_opportunity(
        session_id="s1",
        workspace=tmp_path,
        command="pytest -q",
        cwd=tmp_path,
        exit_code=1,
        stdout="tests/test_calc.py:4: AssertionError: expected 2",
        stderr="",
    )
    assert test_opp is None


def test_python_syntax_and_json_config_diagnostics(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    py = src / "bad.py"
    py.write_text("def x(:\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text("{bad}\n", encoding="utf-8")

    syntax = build_opportunity(
        session_id="s",
        workspace=tmp_path,
        command="python -m py_compile src/bad.py",
        cwd=tmp_path,
        exit_code=1,
        stdout="",
        stderr='  File "src/bad.py", line 1\nSyntaxError: invalid syntax',
    )
    assert syntax is not None
    assert syntax.kind == "syntax"
    assert syntax.implicated_paths == ("src/bad.py",)

    config = build_opportunity(
        session_id="s",
        workspace=tmp_path,
        command="python -m json.tool config.json",
        cwd=tmp_path,
        exit_code=1,
        stdout="",
        stderr="Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
    )
    assert config is not None
    assert config.kind == "config"
    assert config.implicated_paths == ("config.json",)


def test_package_script_name_cannot_hide_non_verifier_command(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text("const x = nope\n", encoding="utf-8")
    _write_ts_package(tmp_path, script="curl https://example.com/deploy")

    opp = build_opportunity(
        session_id="s",
        workspace=tmp_path,
        command="npm run typecheck",
        cwd=tmp_path,
        exit_code=2,
        stdout="src/a.ts(1,11): error TS2304: Cannot find name 'nope'.",
        stderr="",
    )
    assert opp is None


def test_timeout_and_signal_like_failures_are_not_repair_opportunities(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text("const x = nope\n", encoding="utf-8")
    _write_ts_package(tmp_path)
    for exit_code in (124, 137, 143, -9):
        assert (
            build_opportunity(
                session_id="s",
                workspace=tmp_path,
                command="npm run typecheck",
                cwd=tmp_path,
                exit_code=exit_code,
                stdout="src/a.ts(1,11): error TS2304: Cannot find name 'nope'.",
                stderr="",
            )
            is None
        )
