#!/usr/bin/env python3
"""Fail a release build if a public artifact crosses the private-product boundary."""

from __future__ import annotations

import fnmatch
import importlib.util
import io
import sys
import tarfile
import zipfile
from functools import cache
from pathlib import Path
from types import ModuleType

_PRIVATE_ARCHIVE_PREFIXES = ("enterprise/", "services/", "docs-internal/")
_PRIVATE_WHEEL_PREFIXES = ("enterprise/", "lemoncrow_enterprise", "lemoncrow_server/")
_LOCAL_REQUIRED = ("scripts/install.sh", "scripts/bundle.sh", "scripts/local_server.sh")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_REVIEW_ALLOWLIST = _REPO_ROOT / "client" / "review-files.txt"
_RUNTIME_ALLOWLIST = _REPO_ROOT / "client" / "runtime-files.txt"
_LITE_ALLOWLIST = _REPO_ROOT / "client" / "lite-files.txt"
_PRIVATE_OWNED_LOOP_WHEEL_PATHS = (
    "lemoncrow/gateway/cli/coding_engine.py",
    "lemoncrow/gateway/cli/interactive.py",
    "lemoncrow/gateway/cli/runtime.py",
    "lemoncrow/gateway/cli/commands/_private_run.py",
    "lemoncrow/gateway/openai_gateway/",
    "lemoncrow/pro/capabilities/owned_agent_session/gemini_cache.py",
    "lemoncrow/pro/capabilities/owned_agent_session/keepalive.py",
    "lemoncrow/pro/capabilities/owned_agent_session/phase_runner.py",
    "lemoncrow/pro/capabilities/owned_agent_session/stem_prompt.py",
    "integrations/pi/managed.mjs",
)


_PRIVATE_CODE_INTEL_WHEEL_FILES = {
    "lemoncrow/pro/capabilities/code_context/ann_symbol_index.py",
    "lemoncrow/pro/capabilities/code_context/cache.py",
    "lemoncrow/pro/capabilities/code_context/call_graph.py",
    "lemoncrow/pro/capabilities/code_context/call_graph_centrality.py",
    "lemoncrow/pro/capabilities/code_context/edge_resolution.py",
    "lemoncrow/pro/capabilities/code_context/edge_synthesis.py",
    "lemoncrow/pro/capabilities/code_context/embedding.py",
    "lemoncrow/pro/capabilities/code_context/engine.py",
    "lemoncrow/pro/capabilities/code_context/intel_store.py",
    "lemoncrow/pro/capabilities/code_context/lsp_resolver.py",
    "lemoncrow/pro/capabilities/code_context/ppl_rank.py",
    "lemoncrow/pro/capabilities/code_context/ranking.py",
    "lemoncrow/pro/capabilities/code_context/rerank.py",
    "lemoncrow/pro/capabilities/code_context/workspace_router.py",
    # Substrates owned by the private engine (see audit_oss_source_boundary.py).
    "lemoncrow/pro/capabilities/code_context/eval_harness.py",
    "lemoncrow/pro/capabilities/code_context/generated_files.py",
    "lemoncrow/pro/capabilities/code_context/markdown.py",
    "lemoncrow/pro/capabilities/code_context/output_policy.py",
    "lemoncrow/pro/capabilities/code_context/cross_lang/__init__.py",
    "lemoncrow/pro/capabilities/code_context/git_history/__init__.py",
    "lemoncrow/pro/capabilities/code_context/lsp/__init__.py",
    # Their former public homes must not come back either.
    "lemoncrow/infra/code_intel/lsp/__init__.py",
    "lemoncrow/infra/code_intel/markdown.py",
    "lemoncrow/pro/code_intel/cross_lang/__init__.py",
    "lemoncrow/pro/code_intel/git_history/embedder.py",
    "lemoncrow/gateway/mcp_connectors.py",
    "lemoncrow/pro/capabilities/review/change_edits.py",
}


def _fail(message: str) -> None:
    raise SystemExit(f"release boundary audit failed: {message}")


@cache
def _source_rules() -> ModuleType:
    """The content rules of scripts/audit_oss_source_boundary.py."""

    path = Path(__file__).resolve().with_name("audit_oss_source_boundary.py")
    spec = importlib.util.spec_from_file_location("_lemoncrow_oss_source_rules", path)
    if spec is None or spec.loader is None:
        _fail(f"cannot load source boundary rules: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _audit_wheel_content(wheel: zipfile.ZipFile, *, label: str) -> None:
    """Hold every public wheel to the source tree's content rules.

    File allowlists say which modules ship; this says what may be inside them:
    no model/ANN libraries or classes, no private-distribution imports, no
    ANN-index implementations, no learned assets, no model Requires-Dist.
    """

    members = {name: wheel.read(name) for name in wheel.namelist() if not name.endswith("/")}
    violations = _source_rules().audit_wheel(members)
    if violations:
        _fail(f"advanced/private code intelligence in {label} wheel: " + "; ".join(v.render() for v in violations[:10]))


def _normalize(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name


def _regular_members(tf: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in tf.getmembers():
        if not member.isfile():
            continue
        name = _normalize(member.name)
        if name in members:
            _fail(f"duplicate archive member: {name}")
        members[name] = member
    return members


def _audit_member_names(names: list[str], *, local: bool) -> None:
    leaked = sorted(name for name in names if name.startswith(_PRIVATE_ARCHIVE_PREFIXES))
    if local and "scripts/hosted.sh" in names:
        leaked.append("scripts/hosted.sh")
    if leaked:
        _fail(f"private files in public archive: {leaked[:10]}")


def _wheel_bytes(tf: tarfile.TarFile, member: tarfile.TarInfo, *, label: str) -> bytes:
    extracted = tf.extractfile(member)
    if extracted is None:
        _fail(f"cannot read {label} wheel: {member.name}")
    return extracted.read()


def _audit_wheel(tf: tarfile.TarFile, member: tarfile.TarInfo, *, label: str) -> list[str]:
    payload = _wheel_bytes(tf, member, label=label)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as wheel:
            names = wheel.namelist()
            leaked = sorted(name for name in names if name.startswith(_PRIVATE_WHEEL_PREFIXES))
            if leaked:
                _fail(f"private package in {label} wheel: {leaked[:10]}")
            owned_loop = sorted(
                name
                for name in names
                if any(
                    name == path or (path.endswith("/") and name.startswith(path))
                    for path in _PRIVATE_OWNED_LOOP_WHEEL_PATHS
                )
            )
            if owned_loop:
                _fail(f"private owned Runtime execution in {label} wheel: {owned_loop[:10]}")
            _audit_wheel_content(wheel, label=label)
    except zipfile.BadZipFile as exc:
        _fail(f"{label} artifact is not a valid wheel: {member.name}: {exc}")
    return names


def _one(members: dict[str, tarfile.TarInfo], pattern: str, *, label: str) -> tarfile.TarInfo:
    matches = [member for name, member in members.items() if fnmatch.fnmatch(name, pattern)]
    if len(matches) != 1:
        _fail(f"expected exactly one {label} matching {pattern!r}, found {len(matches)}")
    return matches[0]


def audit_local(archive: Path) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        members = _regular_members(tf)
        names = sorted(members)
        _audit_member_names(names, local=True)
        missing = [name for name in _LOCAL_REQUIRED if name not in members]
        if missing:
            _fail(f"local distribution is missing required files: {missing}")
        main = _one(members, "bin/lemoncrow-*.whl", label="local runtime wheel")
        server = _one(members, "server/lemoncrow_server-*.whl", label="public loopback server wheel")
        client = _one(members, "vendor/lemoncrow_client-*.whl", label="public thin-client wheel")
        main_names = _audit_wheel(tf, main, label="local runtime")
        leaked_intelligence = sorted(_PRIVATE_CODE_INTEL_WHEEL_FILES.intersection(main_names))
        if leaked_intelligence:
            _fail(f"private code intelligence in local runtime wheel: {leaked_intelligence[:10]}")
        _audit_wheel(tf, server, label="public loopback server")
        _audit_wheel(tf, client, label="thin client")


def audit_hosted(archive: Path) -> None:
    """Compatibility spelling: the hosted public product is Review-only."""
    audit_review(archive)


def _review_allowed_package_files() -> set[str]:
    try:
        lines = _REVIEW_ALLOWLIST.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        _fail(f"cannot read Review module allowlist: {exc}")
    return {f"lemoncrow_client/{line.strip()}" for line in lines if line.strip() and not line.lstrip().startswith("#")}


def audit_review(archive: Path) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        members = _regular_members(tf)
        names = sorted(members)
        _audit_member_names(names, local=False)
        client = _one(
            members,
            "vendor/lemoncrow_review_client-*-py3-none-any.whl",
            label="Review-only client wheel",
        )
        if len(members) != 1:
            extra = sorted(name for name in members if members[name] is not client)
            _fail(f"Review artifact must contain only the Review wheel; extra files: {extra[:10]}")

        payload = _wheel_bytes(tf, client, label="Review-only client")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as wheel:
                wheel_names = wheel.namelist()
                leaked = sorted(name for name in wheel_names if name.startswith(_PRIVATE_WHEEL_PREFIXES))
                if leaked:
                    _fail(f"private package in Review-only client wheel: {leaked[:10]}")
                _audit_wheel_content(wheel, label="Review-only client")
                package_files = {name for name in wheel_names if name.startswith("lemoncrow_client/")}
                expected = _review_allowed_package_files()
                missing = sorted(expected - package_files)
                unexpected = sorted(package_files - expected)
                if missing:
                    _fail(f"Review wheel missing allowlisted modules: {missing[:10]}")
                if unexpected:
                    _fail(f"Runtime/non-Review modules leaked into Review wheel: {unexpected[:10]}")

                metadata_names = [name for name in wheel_names if name.endswith(".dist-info/METADATA")]
                if len(metadata_names) != 1:
                    _fail("Review wheel must contain exactly one METADATA file")
                metadata = wheel.read(metadata_names[0]).decode("utf-8", errors="replace")
                if "Name: lemoncrow-review-client\n" not in metadata:
                    _fail("Review wheel has the wrong distribution identity")
                if "Requires-Dist:" in metadata:
                    _fail("Review wheel must have zero runtime dependencies")

                entry_names = [name for name in wheel_names if name.endswith(".dist-info/entry_points.txt")]
                if len(entry_names) != 1:
                    _fail("Review wheel must contain exactly one entry_points.txt")
                entry_points = wheel.read(entry_names[0]).decode("utf-8", errors="replace")
                required_entries = {
                    "lc = lemoncrow_client.cli:main",
                    "lcr = lemoncrow_client.cli:review_entrypoint",
                }
                missing_entries = sorted(entry for entry in required_entries if entry not in entry_points)
                if missing_entries:
                    _fail(f"Review wheel missing console entrypoints: {missing_entries}")
        except zipfile.BadZipFile as exc:
            _fail(f"Review-only client artifact is not a valid wheel: {client.name}: {exc}")


def _runtime_allowed_package_files() -> set[str]:
    try:
        lines = _RUNTIME_ALLOWLIST.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        _fail(f"cannot read Runtime module allowlist: {exc}")
    return {f"lemoncrow_client/{line.strip()}" for line in lines if line.strip() and not line.lstrip().startswith("#")}


def _lite_allowed_package_files() -> set[str]:
    try:
        lines = _LITE_ALLOWLIST.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        _fail(f"cannot read Runtime Lite module allowlist: {exc}")
    return {f"lemoncrow_client/{line.strip()}" for line in lines if line.strip() and not line.lstrip().startswith("#")}


def audit_lite(archive: Path) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        members = _regular_members(tf)
        names = sorted(members)
        _audit_member_names(names, local=False)
        client = _one(
            members,
            "vendor/lemoncrow_runtime_lite-*-py3-none-any.whl",
            label="Runtime Lite wheel",
        )
        if len(members) != 1:
            extra = sorted(name for name in members if members[name] is not client)
            _fail(f"Runtime Lite artifact must contain only the Lite wheel; extra files: {extra[:10]}")

        payload = _wheel_bytes(tf, client, label="Runtime Lite")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as wheel:
                wheel_names = wheel.namelist()
                leaked = sorted(name for name in wheel_names if name.startswith(_PRIVATE_WHEEL_PREFIXES))
                if leaked:
                    _fail(f"private package in Runtime Lite wheel: {leaked[:10]}")
                _audit_wheel_content(wheel, label="Runtime Lite")
                package_files = {name for name in wheel_names if name.startswith("lemoncrow_client/")}
                expected = _lite_allowed_package_files()
                missing = sorted(expected - package_files)
                unexpected = sorted(package_files - expected)
                if missing:
                    _fail(f"Runtime Lite wheel missing allowlisted modules: {missing[:10]}")
                if unexpected:
                    _fail(f"non-Lite modules leaked into Runtime Lite wheel: {unexpected[:10]}")

                forbidden = (
                    "lemoncrow_client/mcpserver.py",
                    "lemoncrow_client/session.py",
                    "lemoncrow_client/liveview.py",
                    "lemoncrow_client/autosync.py",
                    "lemoncrow_client/blobs.py",
                    "lemoncrow_client/dispatcher.py",
                    "lemoncrow_client/transport.py",
                    "lemoncrow_client/credentials.py",
                    "lemoncrow_client/authward.py",
                    "lemoncrow_client/config.py",
                )
                present = [name for name in forbidden if name in package_files]
                if present:
                    _fail(f"hosted/client machinery leaked into Runtime Lite wheel: {present}")

                metadata_names = [name for name in wheel_names if name.endswith(".dist-info/METADATA")]
                if len(metadata_names) != 1:
                    _fail("Runtime Lite wheel must contain exactly one METADATA file")
                metadata = wheel.read(metadata_names[0]).decode("utf-8", errors="replace")
                if "Name: lemoncrow-runtime-lite\n" not in metadata:
                    _fail("Runtime Lite wheel has the wrong distribution identity")
                if "Requires-Dist:" in metadata:
                    _fail("Runtime Lite wheel must have zero runtime dependencies")

                entry_names = [name for name in wheel_names if name.endswith(".dist-info/entry_points.txt")]
                if len(entry_names) != 1:
                    _fail("Runtime Lite wheel must contain exactly one entry_points.txt")
                entry_points = wheel.read(entry_names[0]).decode("utf-8", errors="replace")
                if "lemoncrow-runtime-lite-mcp = lemoncrow_client.lite.mcp:serve" not in entry_points:
                    _fail("Runtime Lite wheel missing its stdio MCP entrypoint")
        except zipfile.BadZipFile as exc:
            _fail(f"Runtime Lite artifact is not a valid wheel: {client.name}: {exc}")


def audit_runtime(archive: Path) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        members = _regular_members(tf)
        names = sorted(members)
        _audit_member_names(names, local=False)
        client = _one(
            members,
            "vendor/lemoncrow_runtime_client-*-py3-none-any.whl",
            label="Runtime-only client wheel",
        )
        if len(members) != 1:
            extra = sorted(name for name in members if members[name] is not client)
            _fail(f"Runtime artifact must contain only the Runtime wheel; extra files: {extra[:10]}")

        payload = _wheel_bytes(tf, client, label="Runtime-only client")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as wheel:
                wheel_names = wheel.namelist()
                leaked = sorted(name for name in wheel_names if name.startswith(_PRIVATE_WHEEL_PREFIXES))
                if leaked:
                    _fail(f"private package in Runtime-only client wheel: {leaked[:10]}")
                _audit_wheel_content(wheel, label="Runtime-only client")
                package_files = {name for name in wheel_names if name.startswith("lemoncrow_client/")}
                expected = _runtime_allowed_package_files()
                missing = sorted(expected - package_files)
                unexpected = sorted(package_files - expected)
                if missing:
                    _fail(f"Runtime wheel missing allowlisted modules: {missing[:10]}")
                if unexpected:
                    _fail(f"Review/non-Runtime modules leaked into Runtime wheel: {unexpected[:10]}")

                metadata_names = [name for name in wheel_names if name.endswith(".dist-info/METADATA")]
                if len(metadata_names) != 1:
                    _fail("Runtime wheel must contain exactly one METADATA file")
                metadata = wheel.read(metadata_names[0]).decode("utf-8", errors="replace")
                if "Name: lemoncrow-runtime-client\n" not in metadata:
                    _fail("Runtime wheel has the wrong distribution identity")
                if "Requires-Dist:" in metadata:
                    _fail("Runtime wheel must have zero runtime dependencies")

                entry_names = [name for name in wheel_names if name.endswith(".dist-info/entry_points.txt")]
                if len(entry_names) != 1:
                    _fail("Runtime wheel must contain exactly one entry_points.txt")
                entry_points = wheel.read(entry_names[0]).decode("utf-8", errors="replace")
                required_entries = {
                    "lemoncrow-client = lemoncrow_client.cli:main",
                    "lemoncrow-runtime-client = lemoncrow_client.cli:main",
                }
                missing_entries = sorted(entry for entry in required_entries if entry not in entry_points)
                if missing_entries:
                    _fail(f"Runtime wheel missing console entrypoints: {missing_entries}")
        except zipfile.BadZipFile as exc:
            _fail(f"Runtime-only client artifact is not a valid wheel: {client.name}: {exc}")


#: The license grant the Review engine ships, beside its wheel and inside it (PEP 639 license files).
REVIEW_ENGINE_LICENSE_FILES = ("LICENSE", "LICENSE-APACHE", "NOTICE")
REVIEW_ENGINE_METADATA = (
    "Name: lemoncrow-review-engine",
    "License-Expression: Apache-2.0",
    "Author: LemonCrow",
    *(f"License-File: {name}" for name in REVIEW_ENGINE_LICENSE_FILES),
)


def _audit_review_engine_license(
    tf: tarfile.TarFile, members: dict[str, tarfile.TarInfo], engine: tarfile.TarInfo
) -> None:
    """The engine ships its license: the files at the archive root, identical in the wheel, named in METADATA."""

    shipped: dict[str, bytes] = {}
    for name in REVIEW_ENGINE_LICENSE_FILES:
        member = members.get(name)
        extracted = tf.extractfile(member) if member is not None else None
        if extracted is None:
            _fail(f"Review engine artifact is missing its license file {name}")
            return
        shipped[name] = extracted.read()
        if not shipped[name].strip():
            _fail(f"Review engine license file {name} is empty")
    with zipfile.ZipFile(io.BytesIO(_wheel_bytes(tf, engine, label="Review engine"))) as wheel:
        names = wheel.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            _fail("Review engine wheel must contain exactly one METADATA")
        metadata = wheel.read(metadata_names[0]).decode("utf-8", errors="replace")
        headers = set(metadata.split("\n\n", 1)[0].splitlines())
        missing = [line for line in REVIEW_ENGINE_METADATA if line not in headers]
        if missing:
            _fail(f"Review engine wheel METADATA is missing {missing}")
        dist_info = metadata_names[0].rsplit("/", 1)[0]
        for name, payload in shipped.items():
            inside = f"{dist_info}/licenses/{name}"
            if inside not in names:
                _fail(f"Review engine wheel is missing its license file {inside}")
            if wheel.read(inside) != payload:
                _fail(f"Review engine license file {name} differs between the archive and the wheel")


def audit_review_engine(archive: Path) -> None:
    """Local Review engine: one public wheel, the shared static frontend and its license files."""

    with tarfile.open(archive, "r:gz") as tf:
        members = _regular_members(tf)
        names = sorted(members)
        _audit_member_names(names, local=False)
        engine = _one(
            members,
            "vendor/lemoncrow_review_engine-*-py3-none-any.whl",
            label="Review engine wheel",
        )
        extra = sorted(
            name
            for name in names
            if members[name] is not engine
            and not name.startswith("frontend/")
            and name not in REVIEW_ENGINE_LICENSE_FILES
        )
        if extra:
            _fail(
                "Review engine artifact must contain only its wheel, frontend/ and its license files; "
                f"extra files: {extra[:10]}"
            )
        _audit_wheel(tf, engine, label="Review engine")
        _audit_review_engine_license(tf, members, engine)


def main(argv: list[str]) -> int:
    modes = {"local", "hosted", "review", "review-engine", "runtime", "lite"}
    if len(argv) != 3 or argv[1] not in modes:
        print(
            "usage: audit_release_boundary.py {local|hosted|review|review-engine|runtime|lite} ARCHIVE",
            file=sys.stderr,
        )
        return 2
    archive = Path(argv[2])
    if not archive.is_file():
        _fail(f"archive not found: {archive}")
    if argv[1] == "local":
        audit_local(archive)
    elif argv[1] == "hosted":
        audit_hosted(archive)
    elif argv[1] == "review":
        audit_review(archive)
    elif argv[1] == "review-engine":
        audit_review_engine(archive)
    elif argv[1] == "runtime":
        audit_runtime(archive)
    else:
        audit_lite(archive)
    print(f"✓ Release boundary audit passed: {argv[1]} {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
