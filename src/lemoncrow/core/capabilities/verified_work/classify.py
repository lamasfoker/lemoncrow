"""Deterministic verifier-command and diagnostic classification."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import uuid
from pathlib import Path

from .models import Diagnostic, RepairKind, VerifiedWorkOpportunity

_MAX_DIAGNOSTICS = 64
_MAX_EXCERPT_CHARS = 12_000
_UNSAFE_SHELL_RE = re.compile(r"(?:\|\||(?<!&)&(?!&)|[;|<>`]|\$\(|\n|\r)")
_SAFE_CD_PREFIX_RE = re.compile(r"^\s*cd\s+([^;&|]+?)\s*&&\s*(.+)$", re.DOTALL)
_TS_RE = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s*(?:error|warning)\s+(?P<code>TS\d+):\s*(?P<msg>.+)$"
)
_TS_ALT_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<col>\d+)\s+-\s+(?:error|warning)\s+(?P<code>TS\d+):\s*(?P<msg>.+)$"
)
_RUFF_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<code>[A-Z][A-Z0-9]+)\s+(?P<msg>.+)$")
_MYPY_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?:error|warning):\s*(?P<msg>.+?)(?:\s+\[(?P<code>[^\]]+)\])?$"
)
_PYRIGHT_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?P<col>\d+)\s+-\s+(?:error|warning):\s*(?P<msg>.+)$")
_ESLINT_ROW_RE = re.compile(
    r"^\s*(?P<line>\d+):(?P<col>\d+)\s+(?:error|warning)\s+(?P<msg>.+?)(?:\s{2,}(?P<code>[\w@/.-]+))?\s*$"
)
_GO_RE = re.compile(r"^(?P<path>.+?\.go):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)$")
_JAVA_RE = re.compile(r"^(?P<path>.+?\.java):(?P<line>\d+):\s*(?:error|warning):\s*(?P<msg>.+)$")
_RUST_LOC_RE = re.compile(r"^\s*--?>\s*(?P<path>.+?\.rs):(?P<line>\d+):(?P<col>\d+)\s*$")
_RUST_ERR_RE = re.compile(r"^\s*(?:error|warning)(?:\[(?P<code>[^\]]+)\])?:\s*(?P<msg>.+)$")
_PY_FILE_RE = re.compile(r'^\s*File "(?P<path>.+?\.py)", line (?P<line>\d+)')
_PY_ERROR_RE = re.compile(
    r"^(?P<code>[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)|SyntaxError|IndentationError|TabError):\s*(?P<msg>.+)$"
)
_PYTEST_SHORT_RE = re.compile(
    r"^(?P<path>.+?\.py):(?P<line>\d+):\s*(?P<msg>(?:AssertionError|[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))(?::.*)?)$"
)
_JS_STACK_RE = re.compile(
    r"^\s*(?:at\s+.*?\()?((?P<path>[^()\s]+?\.(?:[cm]?[jt]sx?|vue|svelte))):(?P<line>\d+):(?P<col>\d+)\)?\s*$"
)
_JSON_ERROR_RE = re.compile(r"^(?P<msg>.+?): line (?P<line>\d+) column (?P<col>\d+)(?: \(char \d+\))?$")
_JQ_ERROR_RE = re.compile(r"^parse error: (?P<msg>.+?) at line (?P<line>\d+), column (?P<col>\d+)$")
_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".go",
        ".rs",
        ".java",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".vue",
        ".svelte",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
    }
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _normalized_command_body(command: str) -> str | None:
    body = command.strip()
    match = _SAFE_CD_PREFIX_RE.match(body)
    if match:
        cd_target = match.group(1).strip()
        if not cd_target or cd_target.startswith("/") or ".." in Path(cd_target).parts:
            return None
        body = match.group(2).strip()
    if _UNSAFE_SHELL_RE.search(body) or "&&" in body:
        return None
    return body


def _unwrap(tokens: list[str]) -> list[str]:
    if len(tokens) >= 2 and tokens[:2] in (["uv", "run"], ["poetry", "run"], ["pipenv", "run"]):
        return tokens[2:]
    if tokens and tokens[0] in {"npx", "bunx"}:
        return tokens[1:]
    if len(tokens) >= 2 and tokens[0] in {"pnpm", "yarn", "bun"} and tokens[1] in {"exec", "dlx"}:
        return tokens[2:]
    return tokens


def _diagnostic_cwd(command: str, cwd: Path) -> Path:
    match = _SAFE_CD_PREFIX_RE.match(command.strip())
    if not match:
        return cwd
    raw_target = match.group(1).strip()
    try:
        parts = shlex.split(raw_target)
    except ValueError:
        return cwd
    if len(parts) != 1:
        return cwd
    target = Path(parts[0])
    if target.is_absolute() or ".." in target.parts:
        return cwd
    return (cwd / target).resolve()


def _package_script(command: str, *, cwd: Path) -> tuple[str, str] | None:
    body = _normalized_command_body(command)
    if not body:
        return None
    try:
        tokens = shlex.split(body)
    except ValueError:
        return None
    if (
        len(tokens) < 3
        or tokens[0].lower() not in {"npm", "pnpm", "yarn", "bun"}
        or tokens[1].lower()
        not in {
            "run",
            "run-script",
        }
    ):
        return None
    script = tokens[2]
    try:
        raw = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (script, "")
    scripts = raw.get("scripts") if isinstance(raw, dict) else None
    value = scripts.get(script) if isinstance(scripts, dict) else None
    return (script, str(value or "").strip())


def _package_script_matches_kind(command: str, *, cwd: Path, kind: RepairKind) -> bool:
    resolved = _package_script(command, cwd=cwd)
    if resolved is None:
        return True
    _, script_body = resolved
    return bool(script_body) and classify_command_kind(script_body) == kind


def classify_command_kind(command: str) -> RepairKind | None:
    body = _normalized_command_body(command)
    if not body:
        return None
    try:
        tokens = shlex.split(body)
    except ValueError:
        return None
    if not tokens:
        return None

    lowered = [token.lower() for token in tokens]
    if len(lowered) >= 3 and lowered[0] in {"npm", "pnpm", "yarn", "bun"} and lowered[1] in {"run", "run-script"}:
        script = lowered[2]
        if script in {"typecheck", "type-check", "check-types", "check:type", "types"} or "typecheck" in script:
            return "typecheck"
        if script in {"lint", "lint:check", "check:lint"} or script.startswith("lint:"):
            return "lint"
        return None

    unwrapped = _unwrap(tokens)
    if not unwrapped:
        return None
    exe = Path(unwrapped[0]).name.lower()
    args = [part.lower() for part in unwrapped[1:]]
    if exe in {"tsc", "mypy", "pyright", "basedpyright"}:
        return "typecheck"
    if exe == "ruff" and (not args or args[0] == "check"):
        return "lint"
    if exe == "eslint":
        return "lint"
    if exe == "go" and args:
        if args[0] == "test":
            return "unit_test"
        if args[0] in {"build", "vet"}:
            return "compile"
    if exe == "cargo" and args:
        if args[0] == "test":
            return "unit_test"
        if args[0] in {"check", "build"}:
            return "compile"
    if exe in {"pytest", "py.test"}:
        return "unit_test"
    if exe in {"vitest", "jest"}:
        return "unit_test"
    if exe == "javac":
        return "compile"
    if exe == "jq" and args and args[0] == "empty":
        return "config"
    if exe in {"python", "python3"} and len(args) >= 2 and args[0] == "-m":
        module = args[1]
        if module == "pytest":
            return "unit_test"
        if module in {"py_compile", "compileall"}:
            return "syntax"
        if module == "json.tool":
            return "config"
    return None


def _resolve_diag_path(raw: str, *, cwd: Path, workspace: Path) -> str | None:
    text = raw.strip().strip('"')
    if not text or text.startswith("<"):
        return None
    path = Path(text)
    resolved = path.resolve() if path.is_absolute() else (cwd / path).resolve()
    try:
        return resolved.relative_to(workspace).as_posix()
    except ValueError:
        return None


def _command_file_target(command: str, *, cwd: Path, workspace: Path, kind: RepairKind) -> str | None:
    body = _normalized_command_body(command)
    if not body:
        return None
    try:
        tokens = _unwrap(shlex.split(body))
    except ValueError:
        return None
    if not tokens:
        return None
    exe = Path(tokens[0]).name.lower()
    args = tokens[1:]
    raw = ""
    if kind == "config" and exe in {"python", "python3"} and len(args) >= 3 and args[:2] == ["-m", "json.tool"]:
        raw = args[2]
    elif kind == "config" and exe == "jq" and len(args) >= 2 and args[0] == "empty":
        raw = args[1]
    if not raw or raw == "-":
        return None
    return _resolve_diag_path(raw, cwd=cwd, workspace=workspace)


def parse_diagnostics(
    text: str,
    *,
    kind: RepairKind,
    cwd: Path,
    workspace: Path,
    command: str = "",
) -> tuple[Diagnostic, ...]:
    rows: list[Diagnostic] = []
    eslint_file: str | None = None
    rust_message = ""
    rust_code = "rustc"
    pending_python: tuple[str, int] | None = None

    def add(path_raw: str, line_no: int, col_no: int, code: str, message: str) -> None:
        path = _resolve_diag_path(path_raw, cwd=cwd, workspace=workspace)
        if not path or Path(path).suffix.lower() not in _SOURCE_SUFFIXES:
            return
        rows.append(
            Diagnostic(
                path=path,
                line=max(1, line_no),
                column=max(1, col_no),
                code=code[:64],
                message=message.strip()[:500],
            )
        )

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        match = None
        if kind == "typecheck":
            for pattern in (_TS_RE, _TS_ALT_RE, _MYPY_RE, _PYRIGHT_RE):
                match = pattern.match(line)
                if match:
                    break
        elif kind == "lint":
            match = _RUFF_RE.match(line)
            if not match:
                stripped = line.strip()
                if not line.startswith((" ", "\t")) and Path(stripped).suffix.lower() in {
                    ".js",
                    ".jsx",
                    ".ts",
                    ".tsx",
                    ".mjs",
                    ".cjs",
                    ".vue",
                    ".svelte",
                }:
                    eslint_file = stripped
                    continue
                eslint_row = _ESLINT_ROW_RE.match(line)
                if eslint_row and eslint_file:
                    add(
                        eslint_file,
                        int(eslint_row.group("line")),
                        int(eslint_row.group("col")),
                        eslint_row.group("code") or "eslint",
                        eslint_row.group("msg"),
                    )
                    if len(rows) >= _MAX_DIAGNOSTICS:
                        break
                    continue
        elif kind in {"compile", "unit_test"}:
            match = _GO_RE.match(line) or _JAVA_RE.match(line)
            rust_error = _RUST_ERR_RE.match(line)
            if rust_error:
                rust_message = rust_error.group("msg")
                rust_code = rust_error.group("code") or "rustc"
                continue
            rust_loc = _RUST_LOC_RE.match(line)
            if rust_loc and rust_message:
                add(
                    rust_loc.group("path"),
                    int(rust_loc.group("line")),
                    int(rust_loc.group("col")),
                    rust_code,
                    rust_message,
                )
                if len(rows) >= _MAX_DIAGNOSTICS:
                    break
                continue
            if kind == "unit_test":
                pytest_short = _PYTEST_SHORT_RE.match(line)
                if pytest_short:
                    add(
                        pytest_short.group("path"),
                        int(pytest_short.group("line")),
                        1,
                        "pytest",
                        pytest_short.group("msg"),
                    )
                    if len(rows) >= _MAX_DIAGNOSTICS:
                        break
                    continue
                js_stack = _JS_STACK_RE.match(line)
                if js_stack:
                    add(
                        js_stack.group("path"),
                        int(js_stack.group("line")),
                        int(js_stack.group("col")),
                        "test_stack",
                        "test failure stack frame",
                    )
                    if len(rows) >= _MAX_DIAGNOSTICS:
                        break
                    continue
                py_file = _PY_FILE_RE.match(line)
                if py_file:
                    pending_python = (py_file.group("path"), int(py_file.group("line")))
                    continue
                py_error = _PY_ERROR_RE.match(line.strip())
                if py_error and pending_python:
                    add(pending_python[0], pending_python[1], 1, py_error.group("code"), py_error.group("msg"))
                    pending_python = None
                    if len(rows) >= _MAX_DIAGNOSTICS:
                        break
                    continue
        elif kind in {"syntax", "config"}:
            if kind == "config":
                json_error = _JSON_ERROR_RE.match(line.strip()) or _JQ_ERROR_RE.match(line.strip())
                target = _command_file_target(command, cwd=cwd, workspace=workspace, kind=kind)
                if json_error and target:
                    add(
                        target,
                        int(json_error.group("line")),
                        int(json_error.group("col")),
                        "json",
                        json_error.group("msg"),
                    )
                    if len(rows) >= _MAX_DIAGNOSTICS:
                        break
                    continue
            py_file = _PY_FILE_RE.match(line)
            if py_file:
                pending_python = (py_file.group("path"), int(py_file.group("line")))
                continue
            py_error = _PY_ERROR_RE.match(line.strip())
            if py_error and pending_python:
                add(pending_python[0], pending_python[1], 1, py_error.group("code"), py_error.group("msg"))
                pending_python = None
                if len(rows) >= _MAX_DIAGNOSTICS:
                    break
                continue

        if not match:
            continue
        groups = match.groupdict()
        add(
            groups.get("path") or "",
            int(groups.get("line") or 1),
            int(groups.get("col") or 1),
            str(groups.get("code") or kind),
            str(groups.get("msg") or ""),
        )
        if len(rows) >= _MAX_DIAGNOSTICS:
            break

    unique: list[Diagnostic] = []
    seen: set[tuple[str, int, int, str, str]] = set()
    for row in rows:
        key = (row.path, row.line, row.column, row.code, row.message)
        if key not in seen:
            unique.append(row)
            seen.add(key)
    return tuple(unique)


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def build_opportunity(
    *,
    session_id: str,
    workspace: Path,
    command: str,
    cwd: Path,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> VerifiedWorkOpportunity | None:
    if exit_code == 0 or exit_code < 0 or exit_code in {124, 137, 143}:
        return None
    kind = classify_command_kind(command)
    if kind is None:
        return None
    root = workspace.expanduser().resolve()
    effective_cwd = cwd.expanduser().resolve()
    try:
        effective_cwd.relative_to(root)
    except ValueError:
        return None
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    diagnostic_cwd = _diagnostic_cwd(command, effective_cwd)
    try:
        diagnostic_cwd.relative_to(root)
    except ValueError:
        return None
    if not _package_script_matches_kind(command, cwd=diagnostic_cwd, kind=kind):
        return None
    diagnostics = parse_diagnostics(combined, kind=kind, cwd=diagnostic_cwd, workspace=root, command=command)
    if kind == "unit_test":
        from .policy import _looks_test

        diagnostics = tuple(row for row in diagnostics if not _looks_test(row.path))
    if not diagnostics:
        return None
    implicated = tuple(dict.fromkeys(row.path for row in diagnostics))
    hashes = tuple((rel, _file_hash(root / rel)) for rel in implicated)
    excerpt = combined[-_MAX_EXCERPT_CHARS:]
    identity = f"{session_id}\0{command}\0{_digest(combined)}\0{exit_code}"
    return VerifiedWorkOpportunity(
        id=uuid.uuid5(uuid.NAMESPACE_URL, identity).hex,
        session_id=session_id,
        workspace=str(root),
        command=command,
        cwd=str(effective_cwd),
        kind=kind,
        exit_code=exit_code,
        diagnostics=diagnostics,
        diagnostic_digest=_digest(combined),
        diagnostic_excerpt=excerpt,
        implicated_paths=implicated,
        base_file_hashes=hashes,
        original_result_digest=_digest(f"{exit_code}\0{stdout}\0{stderr}"),
    )


__all__ = ["build_opportunity", "classify_command_kind", "parse_diagnostics"]
