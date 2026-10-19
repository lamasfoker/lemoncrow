#!/usr/bin/env python3
"""Keep LemonCrow release versions and the v0.8+ compatibility floor synchronized."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_PROJECT = Path("pyproject.toml")
PUBLIC_COMPAT_PROJECTS = (Path("server/pyproject.toml"),)
PRIVATE_COMPAT_PROJECTS = (
    Path("enterprise/server/pyproject.toml"),
    Path("enterprise/runtime/pyproject.toml"),
)
CLIENT_PROJECT = Path("client/pyproject.toml")
CLIENT_INIT = Path("client/src/lemoncrow_client/__init__.py")
LEMONCROW_COMPATIBILITY_FLOOR = "0.8.0"
_CLIENT_VERSION_RE = re.compile(r'(?m)^(CLIENT_VERSION: Final\[str\] = ")([^"]+)(")')
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _project_data(repo: Path, relative: Path) -> dict[str, object]:
    return tomllib.loads((repo / relative).read_text(encoding="utf-8"))


def project_version(repo: Path, relative: Path = ROOT_PROJECT) -> str:
    data = _project_data(repo, relative)
    project = data.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("version"), str):
        raise ValueError(f"{relative}: missing [project].version")
    return project["version"]


def lemoncrow_minimum(repo: Path, relative: Path) -> str:
    """Return the declared forward-compatible LemonCrow minimum version."""

    data = _project_data(repo, relative)
    project = data.get("project")
    if not isinstance(project, dict):
        raise ValueError(f"{relative}: missing [project]")
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError(f"{relative}: missing project.dependencies")
    requirements = [
        item.removeprefix("lemoncrow>=")
        for item in dependencies
        if isinstance(item, str) and item.startswith("lemoncrow>=")
    ]
    if len(requirements) != 1:
        raise ValueError(f"{relative}: expected exactly one lemoncrow>= compatibility floor, found {len(requirements)}")
    minimum = requirements[0]
    if not _VERSION_RE.fullmatch(minimum):
        raise ValueError(f"{relative}: LemonCrow compatibility floor must be X.Y.Z, got {minimum!r}")
    return minimum


def _version_tuple(version: str) -> tuple[int, int, int]:
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"version must be X.Y.Z, got {version!r}")
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def client_init_version(repo: Path) -> str:
    match = _CLIENT_VERSION_RE.search((repo / CLIENT_INIT).read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{CLIENT_INIT}: missing CLIENT_VERSION")
    return match.group(2)


def check_versions(repo: Path, expected: str | None = None, *, require_private: bool = False) -> str:
    root_version = project_version(repo)
    wanted = expected or root_version
    if root_version != wanted:
        raise ValueError(f"{ROOT_PROJECT}: version {root_version!r} != expected {wanted!r}")
    if _version_tuple(root_version) < _version_tuple(LEMONCROW_COMPATIBILITY_FLOOR):
        raise ValueError(
            f"{ROOT_PROJECT}: version {root_version!r} predates the v{LEMONCROW_COMPATIBILITY_FLOOR}+ architecture"
        )

    # The thin client's two version sources (wheel metadata, runtime constant)
    # must always agree. They must also match the release version once one is
    # named explicitly (tag check, or the check that ends `set`); a bare `check`
    # between releases only enforces internal consistency.
    client_version = project_version(repo, CLIENT_PROJECT)
    constant = client_init_version(repo)
    if client_version != constant:
        raise ValueError(f"{CLIENT_INIT}: CLIENT_VERSION {constant!r} != {CLIENT_PROJECT} version {client_version!r}")
    if expected is not None and client_version != expected:
        raise ValueError(f"{CLIENT_PROJECT}: version {client_version!r} != expected {expected!r}")

    projects = list(PUBLIC_COMPAT_PROJECTS)
    for relative in PRIVATE_COMPAT_PROJECTS:
        if (repo / relative).exists():
            projects.append(relative)
        elif require_private:
            raise ValueError(f"{relative}: required private package metadata is missing")

    for relative in projects:
        minimum = lemoncrow_minimum(repo, relative)
        if minimum != LEMONCROW_COMPATIBILITY_FLOOR:
            raise ValueError(
                f"{relative}: lemoncrow>={minimum} != compatibility floor >= {LEMONCROW_COMPATIBILITY_FLOOR}"
            )
        if _version_tuple(root_version) < _version_tuple(minimum):
            raise ValueError(f"{relative}: lemoncrow>={minimum} excludes current LemonCrow {root_version}")
    return wanted


def _replace_root_version(text: str, version: str) -> str:
    pattern = re.compile(r'(?ms)(^\[project\]\s*.*?^version\s*=\s*")[^"]+(")')
    updated, count = pattern.subn(rf"\g<1>{version}\2", text, count=1)
    if count != 1:
        raise ValueError("pyproject.toml: could not replace [project].version")
    return updated


def _replace_client_pyproject_version(text: str, version: str) -> str:
    pattern = re.compile(r'(?ms)(^\[project\]\s*.*?^version\s*=\s*")[^"]+(")')
    updated, count = pattern.subn(rf"\g<1>{version}\2", text, count=1)
    if count != 1:
        raise ValueError(f"{CLIENT_PROJECT}: could not replace [project].version")
    return updated


def _replace_client_constant(text: str, version: str) -> str:
    updated, count = _CLIENT_VERSION_RE.subn(rf"\g<1>{version}\3", text, count=1)
    if count != 1:
        raise ValueError(f"{CLIENT_INIT}: could not replace CLIENT_VERSION")
    return updated


def _normalize_compatibility_floor(text: str, *, relative: Path) -> str:
    pattern = re.compile(r'("lemoncrow(?:==|>=))[^";,<>=!~ ]+(")')
    updated, count = pattern.subn(rf'"lemoncrow>={LEMONCROW_COMPATIBILITY_FLOOR}"', text)
    if count != 1:
        raise ValueError(f"{relative}: expected exactly one LemonCrow dependency to normalize, found {count}")
    return updated


def set_versions(repo: Path, version: str) -> None:
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"release version must be X.Y.Z, got {version!r}")
    if _version_tuple(version) < _version_tuple(LEMONCROW_COMPATIBILITY_FLOOR):
        raise ValueError(f"release version {version!r} predates the v{LEMONCROW_COMPATIBILITY_FLOOR}+ architecture")

    root = repo / ROOT_PROJECT
    root_text = _replace_root_version(root.read_text(encoding="utf-8"), version)
    root_text = re.sub(r'("lemoncrow-client==)[^"]+(")', rf"\g<1>{version}\2", root_text)
    root.write_text(root_text, encoding="utf-8")
    client_project = repo / CLIENT_PROJECT
    client_project.write_text(
        _replace_client_pyproject_version(client_project.read_text(encoding="utf-8"), version), encoding="utf-8"
    )
    client_init = repo / CLIENT_INIT
    client_init.write_text(_replace_client_constant(client_init.read_text(encoding="utf-8"), version), encoding="utf-8")
    for relative in (*PUBLIC_COMPAT_PROJECTS, *PRIVATE_COMPAT_PROJECTS):
        path = repo / relative
        if not path.exists():
            if relative in PUBLIC_COMPAT_PROJECTS:
                raise ValueError(f"{relative}: required public package metadata is missing")
            continue
        text = _normalize_compatibility_floor(path.read_text(encoding="utf-8"), relative=relative)
        text = re.sub(r'("lemoncrow-client==)[^"]+(")', rf"\g<1>{version}\2", text)
        path.write_text(text, encoding="utf-8")
    check_versions(repo, version, require_private=any((repo / p).exists() for p in PRIVATE_COMPAT_PROJECTS))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="verify root version and the v0.8+ compatibility floor")
    check.add_argument("version", nargs="?", help="optional expected X.Y.Z version")
    check.add_argument(
        "--require-private",
        action="store_true",
        help="also require the private enterprise server metadata to exist",
    )

    set_cmd = subparsers.add_parser(
        "set", help="set root and thin-client versions and normalize forward-compatible package floors"
    )
    set_cmd.add_argument("version", help="release version in X.Y.Z form")

    args = parser.parse_args(argv)
    try:
        if args.command == "set":
            set_versions(REPO_ROOT, args.version)
            print(f"✓ LemonCrow release version synchronized at {args.version}")
        else:
            version = check_versions(REPO_ROOT, args.version, require_private=args.require_private)
            print(f"✓ LemonCrow release versions agree at {version}")
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"release version check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
