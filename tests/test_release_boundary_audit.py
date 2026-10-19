from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "scripts" / "audit_release_boundary.py"
SOURCE_AUDIT = REPO / "scripts" / "audit_oss_source_boundary.py"


def _run_source_audit(root: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = None
    if root is not None:
        env = {**os.environ, "LEMONCROW_OSS_AUDIT_ROOT": str(root)}
    return subprocess.run(
        [sys.executable, str(SOURCE_AUDIT)],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
    )


def test_oss_source_boundary_audit_passes_current_tree() -> None:
    result = _run_source_audit()
    assert result.returncode == 0, result.stderr
    assert "OSS/private source boundary audit passed" in result.stdout


def test_oss_source_boundary_allows_enterprise_absent_checkout(tmp_path: Path) -> None:
    public = tmp_path / "src" / "lemoncrow" / "pro" / "capabilities" / "code_context"
    public.mkdir(parents=True)
    (public / "__init__.py").write_text("# public contracts only\n", encoding="utf-8")

    result = _run_source_audit(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "OSS/private source boundary audit passed" in result.stdout


def test_oss_source_boundary_rejects_advanced_module_in_public_checkout(tmp_path: Path) -> None:
    public = tmp_path / "src" / "lemoncrow" / "pro" / "capabilities" / "code_context"
    public.mkdir(parents=True)
    (public / "engine.py").write_text("class CodeContextEngine: pass\n", encoding="utf-8")

    result = _run_source_audit(tmp_path)

    assert result.returncode != 0
    assert "advanced/private code intelligence leaked" in result.stderr


def test_oss_source_boundary_rejects_private_only_dependency_in_public_checkout(tmp_path: Path) -> None:
    for case_index, relative in enumerate(
        [
            "src/lemoncrow/pro/capabilities/code_context/output_policy.py",
            "src/lemoncrow/pro/capabilities/code_context/generated_files.py",
            "src/lemoncrow/pro/capabilities/code_context/eval_harness.py",
            "src/lemoncrow/infra/code_intel/lsp/client.py",
            "src/lemoncrow/infra/code_intel/markdown.py",
            "src/lemoncrow/pro/code_intel/cross_lang/edges.py",
            "src/lemoncrow/pro/code_intel/git_history/embedder.py",
            "src/lemoncrow/gateway/mcp_connectors.py",
            "src/lemoncrow/pro/capabilities/review/change_edits.py",
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        leaked = case_tmp_path / relative
        leaked.parent.mkdir(parents=True)
        leaked.write_text("VALUE = 1\n", encoding="utf-8")
        result = _run_source_audit(case_tmp_path)
        assert result.returncode != 0, relative
        assert relative in result.stderr


def test_oss_runtime_imports_and_lite_work_without_enterprise(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "app.py").write_text("def oss_lite_probe():\n    return 1\n", encoding="utf-8")

    code = r"""
from importlib.util import find_spec
from pathlib import Path

assert find_spec("lemoncrow_enterprise_runtime") is None
assert find_spec("lemoncrow.pro.capabilities.code_context.engine") is None

import lemoncrow.gateway.adapters.mcp_server
import lemoncrow.pro.capabilities.review.api
import lemoncrow_client.mcpserver
from lemoncrow.core.capabilities.code_intelligence_provider import (
    private_code_intelligence_available,
)
from lemoncrow_client.lite.index import LiteIndex

assert private_code_intelligence_available() is False
root = Path(__import__("os").environ["LEMONCROW_WORKSPACE_ROOT"])
index = LiteIndex(root)
index.prepare()
assert index.search("oss_lite_probe")["files"][0]["path"] == "app.py"

from click.testing import CliRunner
from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.commands import host_trial as host_trial_commands
from lemoncrow.gateway.cli.commands import runtime as runtime_commands
import lemoncrow_client.host_trial as host_trial

host_trial.run_trial_host = lambda *args, **kwargs: 0
host_trial_commands.run_trial_host = lambda *args, **kwargs: 0

result = CliRunner().invoke(cli, ["runtime", "code", "-p", "hello"])
assert result.exit_code == 1
assert "Hosted LemonCrow coding agent" in result.output

result = CliRunner().invoke(cli, ["with", "claude"])
assert result.exit_code == 0, result.output

host_actions = []
runtime_commands._run_host_script = lambda action, host, **kwargs: host_actions.append((action, host, kwargs))
result = CliRunner().invoke(cli, ["runtime", "enable", "claude", "--dry-run"])
assert result.exit_code == 0, result.output
assert host_actions and host_actions[0][0:2] == ("enable", "claude")

result = CliRunner().invoke(cli, ["runtime", "code", "--pro", "-p", "hello"])
assert result.exit_code == 2
assert "No such option '--pro'" in result.output

result = CliRunner().invoke(cli, ["runtime", "index", "--repo-root", str(root), "--json"])
assert result.exit_code == 0, result.output

from lemoncrow.infra.embeddings.factory import make_code_embedder

assert make_code_embedder("null").__class__.__name__ == "NullEmbedder"

print("oss-lite-ok")
"""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(REPO / "src"), str(REPO / "client" / "src"))),
        "LEMONCROW_WORKSPACE_ROOT": str(repo),
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "oss-lite-ok"


def _wheel(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as wheel:
        for name, payload in entries.items():
            wheel.writestr(name, payload)
    return out.getvalue()


def _archive(path: Path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))


def _run(mode: str, archive: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(AUDIT), mode, str(archive)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )


def _local_entries() -> dict[str, bytes]:
    return {
        "scripts/install.sh": b"#!/bin/sh\n",
        "scripts/bundle.sh": b"#!/bin/sh\n",
        "scripts/local_server.sh": b"#!/bin/sh\n",
        "bin/lemoncrow-1.0.0.whl": _wheel({"lemoncrow/__init__.py": b""}),
        "server/lemoncrow_server-1.0.0.whl": _wheel({"lemoncrow_server_core/__init__.py": b""}),
        "vendor/lemoncrow_client-1.0.0-py3-none-any.whl": _wheel({"lemoncrow_client/__init__.py": b""}),
    }


def test_release_boundary_audit_accepts_checkout_development_dispatcher(tmp_path: Path) -> None:
    entries = _local_entries()
    entries["vendor/lemoncrow_client-1.0.0-py3-none-any.whl"] = _wheel(
        {
            "lemoncrow_client/__init__.py": b"",
            "lemoncrow_client/development.py": (REPO / "client/src/lemoncrow_client/development.py").read_bytes(),
        }
    )
    archive = tmp_path / "local-development-dispatcher.tar.gz"
    _archive(archive, entries)

    result = _run("local", archive)

    assert result.returncode == 0, result.stderr


def test_release_boundary_audit_accepts_public_local_distribution(tmp_path: Path) -> None:
    archive = tmp_path / "local.tar.gz"
    _archive(archive, _local_entries())

    result = _run("local", archive)

    assert result.returncode == 0, result.stderr
    assert "Release boundary audit passed" in result.stdout


def test_release_boundary_audit_rejects_advanced_code_intelligence_inside_public_wheel(tmp_path: Path) -> None:
    entries = _local_entries()
    entries["bin/lemoncrow-1.0.0.whl"] = _wheel(
        {
            "lemoncrow/__init__.py": b"",
            "lemoncrow/pro/capabilities/code_context/engine.py": b"class CodeContextEngine: pass\n",
        }
    )
    archive = tmp_path / "local-code-intelligence-leak.tar.gz"
    _archive(archive, entries)

    result = _run("local", archive)

    assert result.returncode != 0
    assert "private code intelligence" in result.stderr


def test_release_boundary_audit_rejects_private_owned_runtime(tmp_path: Path) -> None:
    for case_index, private_path in enumerate(
        [
            "lemoncrow/gateway/cli/runtime.py",
            "lemoncrow/gateway/cli/coding_engine.py",
            "lemoncrow/gateway/openai_gateway/app.py",
            "lemoncrow/pro/capabilities/owned_agent_session/phase_runner.py",
            "integrations/pi/managed.mjs",
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        entries = _local_entries()
        entries["bin/lemoncrow-1.0.0.whl"] = _wheel(
            {"lemoncrow/__init__.py": b"", private_path: b"# private owned runtime\n"}
        )
        archive = case_tmp_path / "local-owned-runtime-leak.tar.gz"
        _archive(archive, entries)
        result = _run("local", archive)
        assert result.returncode != 0
        assert "private owned Runtime execution" in result.stderr


def test_release_boundary_audit_rejects_private_code_inside_public_wheel(tmp_path: Path) -> None:
    entries = _local_entries()
    entries["bin/lemoncrow-1.0.0.whl"] = _wheel({"lemoncrow_server/private.py": b"secret = True\n"})
    archive = tmp_path / "local-private.tar.gz"
    _archive(archive, entries)

    result = _run("local", archive)

    assert result.returncode != 0
    assert "private package" in result.stderr


def test_release_boundary_audit_rejects_model_or_private_content_in_public_wheel(tmp_path: Path) -> None:
    for case_index, (name, payload) in enumerate(
        [
            ("lemoncrow/infra/rerank_models.py", b"from sentence_transformers import CrossEncoder\n"),
            ("lemoncrow_server_core/index/semantic_ann.py", b"def build(rows):\n    return rows\n"),
            ("lemoncrow/infra/vectors.py", b"import lemoncrow_server.semantic\n"),
            ("lemoncrow/assets/explore_reranker_model.json", b"{}"),
            (
                "lemoncrow-1.0.0.dist-info/METADATA",
                b"Metadata-Version: 2.3\nName: lemoncrow\nRequires-Dist: torch>=2.2; extra == 'semantic'\n",
            ),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        "File allowlists say which modules ship; the content audit says what they may contain."
        entries = _local_entries()
        entries["bin/lemoncrow-1.0.0.whl"] = _wheel({"lemoncrow/__init__.py": b"", name: payload})
        archive = case_tmp_path / "local-content-leak.tar.gz"
        _archive(archive, entries)
        result = _run("local", archive)
        assert result.returncode != 0
        assert "advanced/private code intelligence in local runtime wheel" in result.stderr


_ENGINE_LICENSES = {"LICENSE": b"LemonCrow License\n", "LICENSE-APACHE": b"Apache License\n", "NOTICE": b"LemonCrow\n"}
_ENGINE_METADATA = (
    b"Metadata-Version: 2.4\nName: lemoncrow-review-engine\nVersion: 1.0.0\nAuthor: LemonCrow\n"
    b"License-Expression: Apache-2.0\nLicense-File: LICENSE\nLicense-File: LICENSE-APACHE\nLicense-File: NOTICE\n"
    b"Requires-Dist: aiohttp>=3.14.1\n"
)


def _review_engine_entries(extra_wheel_files: dict[str, bytes] | None = None) -> dict[str, bytes]:
    wheel_files = {
        "lemoncrow_server_core/__init__.py": b"",
        # the public semantic seam may name the private module it resolves
        "lemoncrow_server_core/index/semantic.py": (
            b"from importlib import import_module\n_PRIVATE = 'lemoncrow_server.semantic'\nimport_module(_PRIVATE)\n"
        ),
        "lemoncrow_review_engine-1.0.0.dist-info/METADATA": _ENGINE_METADATA,
        **{f"lemoncrow_review_engine-1.0.0.dist-info/licenses/{name}": text for name, text in _ENGINE_LICENSES.items()},
        **(extra_wheel_files or {}),
    }
    return {
        "vendor/lemoncrow_review_engine-1.0.0-py3-none-any.whl": _wheel(wheel_files),
        "frontend/index.html": b"<!doctype html>\n",
        **_ENGINE_LICENSES,
    }


def test_release_boundary_audit_accepts_review_engine_without_semantic_engine(tmp_path: Path) -> None:
    archive = tmp_path / "review-engine.tar.gz"
    _archive(archive, _review_engine_entries())

    result = _run("review-engine", archive)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "extra",
    [
        {"lemoncrow_server_core/index/semantic_ann.py": b"import numpy\n"},
        {"lemoncrow_server_core/index/layers.py": b"from lemoncrow_server.semantic import SemanticLayer\n"},
    ],
)
def test_release_boundary_audit_rejects_semantic_engine_in_review_engine(
    tmp_path: Path, extra: dict[str, bytes]
) -> None:
    archive = tmp_path / "review-engine-semantic.tar.gz"
    _archive(archive, _review_engine_entries(extra))

    result = _run("review-engine", archive)

    assert result.returncode != 0
    assert "Review engine wheel" in result.stderr


def test_release_boundary_audit_rejects_extra_review_engine_payload(tmp_path: Path) -> None:
    entries = _review_engine_entries()
    entries["vendor/lemoncrow_enterprise_runtime-1.0.0-py3-none-any.whl"] = _wheel({"x.py": b""})
    archive = tmp_path / "review-engine-extra.tar.gz"
    _archive(archive, entries)

    result = _run("review-engine", archive)

    assert result.returncode != 0


def _without_wheel_file(name: str) -> dict[str, bytes]:
    entries = _review_engine_entries()
    wheel_name = "vendor/lemoncrow_review_engine-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(io.BytesIO(entries[wheel_name])) as wheel:
        kept = {item: wheel.read(item) for item in wheel.namelist() if item != name}
    entries[wheel_name] = _wheel(kept)
    return entries


def _with_metadata(metadata: bytes) -> dict[str, bytes]:
    entries = _without_wheel_file("lemoncrow_review_engine-1.0.0.dist-info/METADATA")
    wheel_name = "vendor/lemoncrow_review_engine-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(io.BytesIO(entries[wheel_name])) as wheel:
        kept = {item: wheel.read(item) for item in wheel.namelist()}
    entries[wheel_name] = _wheel({**kept, "lemoncrow_review_engine-1.0.0.dist-info/METADATA": metadata})
    return entries


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("no-archive-notice", "missing its license file NOTICE"),
        ("empty-archive-license", "license file LICENSE is empty"),
        (
            "no-wheel-license",
            "missing its license file lemoncrow_review_engine-1.0.0.dist-info/licenses/LICENSE-APACHE",
        ),
        ("different-wheel-license", "LICENSE differs between the archive and the wheel"),
        ("no-license-expression", "License-Expression: Apache-2.0"),
        ("other-license", "License-Expression: Apache-2.0"),
        ("no-author", "Author: LemonCrow"),
        ("unlisted-license-file", "License-File: NOTICE"),
    ],
)
def test_release_boundary_audit_rejects_a_review_engine_without_its_license(
    tmp_path: Path, case: str, expected: str
) -> None:
    if case == "no-archive-notice":
        entries = _review_engine_entries()
        del entries["NOTICE"]
    elif case == "empty-archive-license":
        entries = {**_review_engine_entries(), "LICENSE": b"\n"}
    elif case == "no-wheel-license":
        entries = _without_wheel_file("lemoncrow_review_engine-1.0.0.dist-info/licenses/LICENSE-APACHE")
    elif case == "different-wheel-license":
        entries = {**_review_engine_entries(), "LICENSE": b"Some other license\n"}
    elif case == "no-license-expression":
        entries = _with_metadata(_ENGINE_METADATA.replace(b"License-Expression: Apache-2.0\n", b""))
    elif case == "other-license":
        entries = _with_metadata(_ENGINE_METADATA.replace(b"Apache-2.0", b"LicenseRef-Proprietary"))
    elif case == "no-author":
        entries = _with_metadata(_ENGINE_METADATA.replace(b"Author: LemonCrow\n", b""))
    else:
        entries = _with_metadata(_ENGINE_METADATA.replace(b"License-File: NOTICE\n", b""))
    archive = tmp_path / f"review-engine-{case}.tar.gz"
    _archive(archive, entries)

    result = _run("review-engine", archive)

    assert result.returncode != 0
    assert expected in result.stderr, result.stderr


def _review_wheel_entries() -> dict[str, bytes]:
    allowlist = [
        line.strip()
        for line in (REPO / "client" / "review-files.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    entries = {f"lemoncrow_client/{name}": b"" for name in allowlist}
    entries["lemoncrow_review_client-1.0.0.dist-info/METADATA"] = (
        b"Metadata-Version: 2.3\nName: lemoncrow-review-client\nVersion: 1.0.0\n"
    )
    entries["lemoncrow_review_client-1.0.0.dist-info/entry_points.txt"] = (
        b"[console_scripts]\nlc = lemoncrow_client.cli:main\nlcr = lemoncrow_client.cli:review_entrypoint\n"
    )
    return entries


def test_release_boundary_audit_accepts_hosted_review_only_client(tmp_path: Path) -> None:
    archive = tmp_path / "hosted.tar.gz"
    _archive(
        archive,
        {"vendor/lemoncrow_review_client-1.0.0-py3-none-any.whl": _wheel(_review_wheel_entries())},
    )

    result = _run("hosted", archive)

    assert result.returncode == 0, result.stderr


def test_release_boundary_audit_rejects_old_combined_client_as_hosted(tmp_path: Path) -> None:
    archive = tmp_path / "hosted-combined.tar.gz"
    _archive(
        archive,
        {"vendor/lemoncrow_client-1.0.0-py3-none-any.whl": _wheel({"lemoncrow_client/__init__.py": b""})},
    )

    result = _run("hosted", archive)

    assert result.returncode != 0
    assert "Review-only client wheel" in result.stderr


def test_release_boundary_audit_rejects_extra_hosted_payload(tmp_path: Path) -> None:
    archive = tmp_path / "hosted-extra.tar.gz"
    _archive(
        archive,
        {
            "vendor/lemoncrow_review_client-1.0.0-py3-none-any.whl": _wheel(_review_wheel_entries()),
            "enterprise/server.py": b"private = True\n",
        },
    )

    result = _run("hosted", archive)

    assert result.returncode != 0
    assert "private files" in result.stderr


def test_release_boundary_audit_accepts_review_only_client(tmp_path: Path) -> None:
    archive = tmp_path / "review.tar.gz"
    _archive(
        archive,
        {"vendor/lemoncrow_review_client-1.0.0-py3-none-any.whl": _wheel(_review_wheel_entries())},
    )

    result = _run("review", archive)

    assert result.returncode == 0, result.stderr
    assert "Release boundary audit passed" in result.stdout


def test_release_boundary_audit_rejects_runtime_module_in_review_wheel(tmp_path: Path) -> None:
    entries = _review_wheel_entries()
    entries["lemoncrow_client/mcpserver.py"] = b"runtime = True\n"
    archive = tmp_path / "review-runtime-leak.tar.gz"
    _archive(
        archive,
        {"vendor/lemoncrow_review_client-1.0.0-py3-none-any.whl": _wheel(entries)},
    )

    result = _run("review", archive)

    assert result.returncode != 0
    assert "Runtime/non-Review modules leaked" in result.stderr


def test_release_boundary_audit_rejects_review_runtime_dependency(tmp_path: Path) -> None:
    entries = _review_wheel_entries()
    entries["lemoncrow_review_client-1.0.0.dist-info/METADATA"] += b"Requires-Dist: lemoncrow-runtime\n"
    archive = tmp_path / "review-dependency.tar.gz"
    _archive(
        archive,
        {"vendor/lemoncrow_review_client-1.0.0-py3-none-any.whl": _wheel(entries)},
    )

    result = _run("review", archive)

    assert result.returncode != 0
    assert "zero runtime dependencies" in result.stderr


def _runtime_wheel_entries() -> dict[str, bytes]:
    allowlist = [
        line.strip()
        for line in (REPO / "client" / "runtime-files.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    entries = {f"lemoncrow_client/{name}": b"" for name in allowlist}
    entries["lemoncrow_runtime_client-1.0.0.dist-info/METADATA"] = (
        b"Metadata-Version: 2.3\nName: lemoncrow-runtime-client\nVersion: 1.0.0\n"
    )
    entries["lemoncrow_runtime_client-1.0.0.dist-info/entry_points.txt"] = (
        b"[console_scripts]\n"
        b"lemoncrow-client = lemoncrow_client.cli:main\n"
        b"lemoncrow-runtime-client = lemoncrow_client.cli:main\n"
    )
    return entries


def test_release_boundary_audit_accepts_runtime_only_client(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    _archive(
        archive,
        {"vendor/lemoncrow_runtime_client-1.0.0-py3-none-any.whl": _wheel(_runtime_wheel_entries())},
    )

    result = _run("runtime", archive)

    assert result.returncode == 0, result.stderr
    assert "Release boundary audit passed" in result.stdout


def test_release_boundary_audit_rejects_review_module_in_runtime_wheel(tmp_path: Path) -> None:
    entries = _runtime_wheel_entries()
    entries["lemoncrow_client/review.py"] = b"review = True\n"
    archive = tmp_path / "runtime-review-leak.tar.gz"
    _archive(
        archive,
        {"vendor/lemoncrow_runtime_client-1.0.0-py3-none-any.whl": _wheel(entries)},
    )

    result = _run("runtime", archive)

    assert result.returncode != 0
    assert "Review/non-Runtime modules leaked" in result.stderr


def test_release_boundary_audit_rejects_runtime_dependency(tmp_path: Path) -> None:
    entries = _runtime_wheel_entries()
    entries["lemoncrow_runtime_client-1.0.0.dist-info/METADATA"] += b"Requires-Dist: lemoncrow-review-client\n"
    archive = tmp_path / "runtime-dependency.tar.gz"
    _archive(
        archive,
        {"vendor/lemoncrow_runtime_client-1.0.0-py3-none-any.whl": _wheel(entries)},
    )

    result = _run("runtime", archive)

    assert result.returncode != 0
    assert "zero runtime dependencies" in result.stderr


@pytest.mark.parametrize("allowlist", ["runtime-files.txt", "review-files.txt", "lite-files.txt"])
def test_client_artifact_allowlist_is_closed_under_relative_imports(allowlist: str) -> None:
    """Every module an allowlisted client module imports at load time must ship with it.

    Only unconditional module-level imports count: lazy imports inside functions or
    guarded blocks are how artifacts deliberately leave optional surfaces out.
    """

    package = REPO / "client" / "src" / "lemoncrow_client"
    listed = {
        line.strip()
        for line in (REPO / "client" / allowlist).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing: set[str] = set()
    for relative in sorted(name for name in listed if name.endswith(".py")):
        tree = ast.parse((package / relative).read_text(encoding="utf-8"))
        here = Path(relative).parent
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.level == 0:
                continue
            base = here
            for _ in range(node.level - 1):
                base = base.parent
            stems = [base / Path(*node.module.split("."))] if node.module else []
            if not node.module:
                stems = [base / alias.name for alias in node.names]
            for stem in stems:
                candidates = {f"{stem.as_posix()}.py", f"{stem.as_posix()}/__init__.py"}
                if (package / f"{stem}.py").is_file() or (package / stem / "__init__.py").is_file():
                    if not candidates & listed:
                        missing.add(f"{relative} -> {stem.as_posix()}")
    assert not missing, f"{allowlist} omits modules its members import: {sorted(missing)}"
