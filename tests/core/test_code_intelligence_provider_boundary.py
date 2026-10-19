from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities import code_intelligence_provider as provider

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    provider.private_code_intelligence_available(),
    reason="asserts OSS behaviour; the private provider is on the path",
)
def test_public_provider_fails_closed_without_private_runtime() -> None:
    assert provider.private_code_intelligence_available() is False
    with pytest.raises(provider.ProEngineUnavailable, match="Private code intelligence is not installed/available"):
        provider.create_code_intelligence_provider(REPO)
