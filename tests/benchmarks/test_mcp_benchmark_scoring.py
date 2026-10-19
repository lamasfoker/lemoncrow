from __future__ import annotations

import ast
import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_benchmarks_pkg = types.ModuleType("benchmarks")
_benchmarks_pkg.__path__ = [str(ROOT / "benchmarks")]
_mcp_pkg = types.ModuleType("benchmarks.mcp_tools")
_mcp_pkg.__path__ = [str(ROOT / "benchmarks" / "mcp_tools")]
sys.modules["benchmarks"] = _benchmarks_pkg
sys.modules["benchmarks.mcp_tools"] = _mcp_pkg

harness = _load_module("benchmarks.mcp_tools.harness", ROOT / "benchmarks" / "mcp_tools" / "harness.py")
reporter = _load_module("benchmarks.mcp_tools.reporter", ROOT / "benchmarks" / "mcp_tools" / "reporter.py")

sys.modules.pop("benchmarks.mcp_tools.reporter", None)
sys.modules.pop("benchmarks.mcp_tools.harness", None)
sys.modules.pop("benchmarks.mcp_tools", None)
sys.modules.pop("benchmarks", None)

BenchCase = harness.BenchCase
CaseResult = harness.CaseResult
ToolReport = harness.ToolReport
render_summary = reporter.render_summary


def test_code_matrix_quality_floor_is_enforced() -> None:
    floor = 0.6
    default = BenchCase.__dataclass_fields__["quality_score"].default
    assert default >= floor, "the default benchmark quality score must stay at or above the floor"

    # The code matrix is expensive to import because importing it intentionally
    # discovers hundreds of generated repo-backed benchmark cases. This guard is
    # about configuration, not discovery: validate every BenchCase construction
    # statically and let the benchmark runner cover dynamic case generation.
    source = (ROOT / "benchmarks" / "mcp_tools" / "cases" / "code.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "BenchCase"
    ]
    assert calls, "code benchmark matrix must contain BenchCase definitions"
    quality_score_position = list(BenchCase.__dataclass_fields__).index("quality_score")
    for call in calls:
        assert all(
            item.arg is not None for item in call.keywords
        ), "BenchCase **kwargs can hide quality_score; keep benchmark quality configuration statically auditable"
        if len(call.args) > quality_score_position:
            value = call.args[quality_score_position]
            assert isinstance(value, ast.Constant) and isinstance(
                value.value, (int, float)
            ), "positional BenchCase quality_score overrides must be static numeric literals"
            assert float(value.value) >= floor, "all code benchmark quality scores must stay at or above 0.6"

        keyword = next((item for item in call.keywords if item.arg == "quality_score"), None)
        if keyword is None:
            continue
        assert isinstance(keyword.value, ast.Constant) and isinstance(
            keyword.value.value, (int, float)
        ), "code benchmark quality_score overrides must be static numeric literals so the floor remains auditable"
        assert float(keyword.value.value) >= floor, "all code benchmark quality scores must stay at or above 0.6"


def test_effective_tokens_use_quality_floor() -> None:
    case = BenchCase(op="search", args={}, quality_score=0.4, label="search")
    result = CaseResult(
        case=case,
        response={},
        lemoncrow_tokens=100,
        baseline_tokens=200,
        quality_score=case.quality_score,
        input_file_tokens=0,
        baseline_commands=[],
        spill_probe_tokens=0,
        spill_probe_hits=0,
        elapsed_ms=1.0,
        passed=True,
    )

    assert result.effective_tokens == 250


def test_tool_report_accumulates_effective_tokens() -> None:
    case = BenchCase(op="search", args={}, quality_score=0.5, label="search")
    result = CaseResult(
        case=case,
        response={},
        lemoncrow_tokens=80,
        baseline_tokens=200,
        quality_score=case.quality_score,
        input_file_tokens=0,
        baseline_commands=[],
        spill_probe_tokens=0,
        spill_probe_hits=0,
        elapsed_ms=1.0,
        passed=True,
    )
    report = ToolReport(tool_name="code", results=[result])

    assert report.total_effective_tokens == 160
    assert report.avg_effective_tokens == 160


def test_render_summary_includes_effective_tokens() -> None:
    case = BenchCase(op="search", args={}, quality_score=1.0, label="search")
    result = CaseResult(
        case=case,
        response={},
        lemoncrow_tokens=42,
        baseline_tokens=100,
        quality_score=case.quality_score,
        input_file_tokens=0,
        baseline_commands=[],
        spill_probe_tokens=0,
        spill_probe_hits=0,
        elapsed_ms=1.0,
        passed=True,
    )

    summary = render_summary([ToolReport(tool_name="code", results=[result])])

    assert "effective tokens" in summary
    assert "effective:" in summary
