"""scripts/audit_oss_source_boundary.py: content-aware public/private boundary rules.

Each case writes a miniature checkout and asserts the rule that must fire (or
stay quiet). Equivalent implementations under unexpected names or paths are the
point: the audit is not a list of known filenames.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_AUDIT = Path(__file__).resolve().parents[1] / "scripts" / "audit_oss_source_boundary.py"
_spec = importlib.util.spec_from_file_location("_oss_source_audit_under_test", _AUDIT)
assert _spec and _spec.loader
audit = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = audit  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(audit)


def _checkout(root: Path, files: dict[str, str]) -> list[str]:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return audit.public_files(root)


def _rules(root: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    return {(item.path, item.rule) for item in audit.audit_paths(root, _checkout(root, files))}


_PUBLIC_PKG = {
    "src/lemoncrow/__init__.py": "",
    "src/lemoncrow/pro/__init__.py": "",
    "src/lemoncrow/pro/capabilities/__init__.py": "",
    "src/lemoncrow/pro/capabilities/code_context/__init__.py": "# public contracts\n",
}


def test_clean_public_tree_passes(tmp_path: Path) -> None:
    assert _rules(tmp_path, {**_PUBLIC_PKG, "src/lemoncrow/util.py": "import json\nimport numpy\n"}) == set()


def test_model_libraries_are_rejected_under_any_file_name(tmp_path: Path) -> None:
    for case_index, source in enumerate(
        [
            "from sentence_transformers import CrossEncoder\n",
            "import torch\n",
            "def load():\n    import transformers\n    return transformers\n",
            "import importlib\nimportlib.import_module('hnswlib')\n",
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        rules = _rules(case_tmp_path, {**_PUBLIC_PKG, "benchmarks/tools/helpers.py": source})
        assert ("benchmarks/tools/helpers.py", "model-library") in rules


def test_model_code_embedded_in_a_python_c_helper_is_rejected(tmp_path: Path) -> None:
    helper = '_HELPER = """\nimport json\nfrom sentence_transformers import SentenceTransformer\n"""\n'
    rules = _rules(tmp_path, {**_PUBLIC_PKG, "benchmarks/bench_latency.py": helper})
    assert ("benchmarks/bench_latency.py", "model-library") in rules


def test_model_classes_are_rejected_even_without_their_import(tmp_path: Path) -> None:
    rules = _rules(tmp_path, {**_PUBLIC_PKG, "src/lemoncrow/rank.py": "def rank(lib):\n    return lib.CrossEncoder\n"})
    assert ("src/lemoncrow/rank.py", "model-class") in rules


def test_private_distribution_imports_are_rejected_outside_the_seams(tmp_path: Path) -> None:
    files = {
        **_PUBLIC_PKG,
        "server/src/lemoncrow_server_core/__init__.py": "",
        "server/src/lemoncrow_server_core/index/__init__.py": "",
        "server/src/lemoncrow_server_core/index/layers.py": "from lemoncrow_server.semantic import SemanticLayer\n",
        # the reviewed seam may resolve the private module by name
        "server/src/lemoncrow_server_core/index/semantic.py": (
            "from importlib import import_module\n_M = 'lemoncrow_server.semantic'\nimport_module(_M)\n"
        ),
    }
    rules = _rules(tmp_path, files)
    assert ("server/src/lemoncrow_server_core/index/layers.py", "private-import") in rules
    assert not {rule for path, rule in rules if path.endswith("index/semantic.py")}


def test_private_module_strings_are_rejected_but_probes_and_absence_checks_are_not(tmp_path: Path) -> None:
    files = {
        **_PUBLIC_PKG,
        "tests/test_patch.py": "def test(m):\n    m.setattr('lemoncrow_enterprise_runtime.code_intelligence.provider.X', 1)\n",
        "tests/test_probe.py": (
            "import importlib.util\n"
            "AVAILABLE = importlib.util.find_spec('lemoncrow_server') is not None\n"
            "def test(text):\n    assert 'lemoncrow_server.ops' not in text\n"
        ),
    }
    rules = _rules(tmp_path, files)
    assert ("tests/test_patch.py", "private-module-string") in rules
    assert not {rule for path, rule in rules if path == "tests/test_probe.py"}


def test_engine_module_contributed_into_a_public_namespace_is_rejected(tmp_path: Path) -> None:
    """``from <public pkg> import engine`` where only the private tree defines it."""

    files = {
        **_PUBLIC_PKG,
        "enterprise/runtime/src/lemoncrow/pro/capabilities/code_context/engine.py": "class CodeContextEngine: ...\n",
        "tests/core/test_engine.py": "from lemoncrow.pro.capabilities.code_context import engine as eng\n",
        "tests/core/test_direct.py": "from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine\n",
        "src/lemoncrow/pro/capabilities/code_context/explore.py": "from .engine import CodeContextEngine\n",
    }
    # enterprise/ is not public: restrict the public set the way the mirror would.
    _checkout(tmp_path, files)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "public-paths.txt").write_text("src/\ntests/\n", encoding="utf-8")
    public = audit.public_files(tmp_path)
    assert not any(path.startswith("enterprise/") for path in public)
    rules = {(item.path, item.rule) for item in audit.audit_paths(tmp_path, public)}
    assert ("tests/core/test_engine.py", "private-import") in rules
    assert ("tests/core/test_direct.py", "private-import") in rules
    assert ("src/lemoncrow/pro/capabilities/code_context/explore.py", "private-import") in rules


def test_engine_import_is_rejected_in_a_public_checkout_without_enterprise(tmp_path: Path) -> None:
    files = {
        **_PUBLIC_PKG,
        "integrations/hooks/prompt.py": "from lemoncrow.pro.capabilities.code_context.engine import X\n",
    }
    assert ("integrations/hooks/prompt.py", "private-import") in _rules(tmp_path, files)


@pytest.mark.parametrize(
    ("rel", "source"),
    [
        ("src/lemoncrow/infra/vectors.py", "class HnswGraph:\n    pass\n"),
        ("src/lemoncrow/infra/vectors.py", "def build_faiss_index(rows):\n    return rows\n"),
        ("src/lemoncrow/infra/knn_search.py", ""),
    ],
)
def test_ann_index_implementations_are_rejected(tmp_path: Path, rel: str, source: str) -> None:
    assert (rel, "ann-index") in _rules(tmp_path, {**_PUBLIC_PKG, rel: source})


def test_reviewed_ann_allowlist_is_exact(tmp_path: Path) -> None:
    files = {
        **_PUBLIC_PKG,
        "src/lemoncrow/pro/capabilities/archival_recall/ann.py": "class ArchivalAnnIndex: ...\n",
        "src/lemoncrow/pro/capabilities/archival_recall/other.py": "class ArchivalAnnIndex: ...\n",
    }
    rules = _rules(tmp_path, files)
    assert ("src/lemoncrow/pro/capabilities/archival_recall/other.py", "ann-index") in rules
    assert not {rule for path, rule in rules if path.endswith("archival_recall/ann.py")}


def test_model_assets_are_rejected(tmp_path: Path) -> None:
    for case_index, name in enumerate(
        ["weights.safetensors", "model.onnx", "explore_reranker_model.json", "vectors.npy", "codebase_lambdamart.json"]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        assert (f"src/lemoncrow/assets/{name}", "model-asset") in _rules(
            case_tmp_path, {**_PUBLIC_PKG, f"src/lemoncrow/assets/{name}": "{}"}
        )


def test_public_manifests_may_not_depend_on_model_libraries(tmp_path: Path) -> None:
    pyproject = (
        '[project]\nname = "x"\ndependencies = ["click"]\n'
        '[project.optional-dependencies]\nsemantic = ["sentence-transformers>=3,<5", "Torch>=2"]\n'
        '[dependency-groups]\ndev = ["XGBoost>=3"]\n'
    )
    files = {
        **_PUBLIC_PKG,
        "pyproject.toml": pyproject,
        "benchmarks/requirements.txt": "numpy\nfaiss_cpu==1.8\n",
    }
    details = {(item.path, item.detail) for item in audit.audit_paths(tmp_path, _checkout(tmp_path, files))}
    assert ("pyproject.toml", "depends on sentence-transformers") in details
    assert ("pyproject.toml", "depends on Torch") in details
    assert ("pyproject.toml", "depends on XGBoost") in details
    assert ("benchmarks/requirements.txt", "depends on faiss_cpu") in details


def test_mirror_denies_scope_the_audit(tmp_path: Path) -> None:
    files = {
        **_PUBLIC_PKG,
        "tests/private/test_engine.py": "import torch\n",
        "tests/core/test_public.py": "import torch\n",
    }
    _checkout(tmp_path, files)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "public-paths.txt").write_text("src/\ntests/\n!tests/private/\n", encoding="utf-8")
    rules = {(item.path, item.rule) for item in audit.audit_paths(tmp_path, audit.public_files(tmp_path))}
    assert ("tests/core/test_public.py", "model-library") in rules
    assert not {rule for path, rule in rules if path.startswith("tests/private/")}
