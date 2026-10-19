"""Unit tests for the canonical language registry (DLS-LANG-01/02/03/04)."""

from __future__ import annotations

import dataclasses

import pytest

from lemoncrow.infra.code_intel.languages import (
    ALL_LANGUAGES,
    EXTENSION_TO_LANGUAGE,
    FILENAME_TO_LANGUAGE,
    Language,
    language_by_name,
    language_for_path,
)
from lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast import _LANG_CONFIG

_LEGACY_EXTENSION_TO_NAME: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".sql": "sql",
    ".md": "markdown",
    ".markdown": "markdown",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".rb": "ruby",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".c": "c",
    ".h": "c",
    ".swift": "swift",
    ".php": "php",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
}


def test_language_registry_surface_contract() -> None:
    """case: test_public_surface_types"""
    assert dataclasses.is_dataclass(Language)
    fields = {f.name for f in dataclasses.fields(Language)}
    assert fields == {"name", "extensions", "parser_name", "filenames"}
    sample = language_by_name("python")
    assert sample is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        sample.name = "mutated"
    "case: test_public_surface_callables_and_containers"
    assert callable(language_for_path)
    assert callable(language_by_name)
    assert isinstance(EXTENSION_TO_LANGUAGE, dict)
    assert isinstance(FILENAME_TO_LANGUAGE, dict)
    assert isinstance(ALL_LANGUAGES, frozenset)
    assert "bash" in ALL_LANGUAGES


def test_language_resolution_contract() -> None:
    """case: test_legacy_extensions_resolve_to_canonical_name"""
    for extension, expected_name in sorted(_LEGACY_EXTENSION_TO_NAME.items()):
        resolved = language_for_path(f"file{extension}")
        assert resolved is not None, f"extension {extension} dropped from registry"
        assert resolved.name == expected_name
    "case: test_unknown_extension_resolves_to_none"
    assert language_for_path("file.xyz") is None
    "case: test_unregistered_no_extension_resolves_to_none"
    assert language_for_path("noext") is None
    "case: test_registered_extensionless_filenames_resolve_by_basename"
    for filename in ["GNUmakefile", "Makefile", "makefile"]:
        resolved = language_for_path(f"subdir/{filename}")
        assert resolved is not None
        assert resolved.name == "make"
    "case: test_shell_extensions_canonical_to_bash"
    for extension in [".sh", ".bash", ".zsh"]:
        resolved = language_for_path(f"script{extension}")
        assert resolved is not None
        assert resolved.name == "bash"
    "case: test_csharp_canonical_spelling"
    resolved = language_for_path("Program.cs")
    assert resolved is not None
    assert resolved.name == "csharp"


def test_language_registry_drift_contract() -> None:
    """case: test_lang_config_keys_present_in_registry"""
    for lang_key in sorted(_LANG_CONFIG.keys()):
        assert (
            language_by_name(lang_key) is not None
        ), f"_LANG_CONFIG key {lang_key!r} is absent from the canonical registry"
