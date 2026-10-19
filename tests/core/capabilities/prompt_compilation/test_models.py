"""Contract tests for prompt_compilation.models."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

import pytest

from lemoncrow.pro.capabilities.prompt_compilation.models import (
    COUNTEREXAMPLE_METADATA_KEY,
    DEFAULT_STABILITY,
    BlockKind,
    PromptBlock,
    Stability,
)


def _block(
    *,
    id: str = "test/block",
    kind: BlockKind = BlockKind.SYSTEM,
    content: str = "hello world",
    stability: Stability = Stability.STATIC,
    cacheable: bool = True,
    metadata: Mapping[str, Any] | None = None,
    stability_override_reason: str | None = None,
) -> PromptBlock:
    return PromptBlock(
        id=id,
        kind=kind,
        content=content,
        stability=stability,
        cacheable=cacheable,
        metadata=metadata or {},
        stability_override_reason=stability_override_reason,
    )


def test_default_stability_contract() -> None:
    assert all(kind in DEFAULT_STABILITY for kind in BlockKind)
    assert {
        BlockKind.TOOL_SCHEMA: Stability.STATIC,
        BlockKind.REPO_SUMMARY: Stability.SESSION,
        BlockKind.PLAYBOOK: Stability.BRANCH,
        BlockKind.USER_TASK: Stability.TURN,
        BlockKind.SCRATCHPAD: Stability.VOLATILE,
    }.items() <= DEFAULT_STABILITY.items()


def test_stability_override_contract() -> None:
    with pytest.raises(ValueError, match="stability_override_reason"):
        _block(kind=BlockKind.SYSTEM, stability=Stability.SESSION)
    overridden = _block(
        kind=BlockKind.SYSTEM, stability=Stability.SESSION, stability_override_reason="test suite override"
    )
    assert overridden.stability == Stability.SESSION
    assert _block(kind=BlockKind.SYSTEM, stability=Stability.STATIC).stability_override_reason is None


def test_version_hash_contract() -> None:
    assert _block(content="hello world").version_hash == sha256(b"hello world").hexdigest()
    assert _block(content="hello world").version_hash != _block(content="hello world!").version_hash
    assert _block(content="stable content").version_hash == _block(content="stable content").version_hash


def test_cacheability_contract() -> None:
    assert _block(kind=BlockKind.USER_TASK, stability=Stability.TURN, cacheable=True).cacheable is False
    assert _block(kind=BlockKind.SCRATCHPAD, stability=Stability.VOLATILE, cacheable=True).cacheable is False
    assert _block(kind=BlockKind.SYSTEM, stability=Stability.STATIC, cacheable=True).cacheable is True


def test_id_and_content_validation_contract() -> None:
    for value in ["tools/v1", "sys.core", "block:42", "a-b_c"]:
        assert _block(id=value).id == value
    with pytest.raises(ValueError, match="invalid"):
        _block(id="UPPER_CASE")
    with pytest.raises(ValueError, match="content"):
        _block(content="")


def test_counterexample_marker_contract() -> None:
    marked = _block(kind=BlockKind.TOOL_RESULT, stability=Stability.TURN, metadata={COUNTEREXAMPLE_METADATA_KEY: True})
    assert marked.is_counterexample is True
    assert _block().is_counterexample is False
