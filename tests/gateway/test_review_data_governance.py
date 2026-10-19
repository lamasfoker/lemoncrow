"""RC6: operator-attested data governance (route trust, provider terms, storage region, egress).

The document is strict and all-or-nothing; anything not attested stays
``unknown``; an unreadable document fails closed for every route whose trust
it could change; and neither its path nor its values nor its error text ever
leaves the parser except validated labels.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.data_governance import (
    DATA_GOVERNANCE_ENV,
    MAX_DOCUMENT_BYTES,
    NOT_CONFIGURED,
    DataGovernanceAttestations,
    RouteAttestation,
    data_governance_digest,
    load_data_governance,
    process_route_trust,
    route_governance,
)
from lemoncrow.pro.capabilities.review.rules import ContentAccessRule, content_access_for_path

CANARY = "canary-91c3e0d7b4"
_WITHHELD = "could not be read ({kind}); error text withheld: it may quote the file"

_FULL: dict[str, Any] = {
    "version": 1,
    "storage": {"region": "eu-west-1"},
    "network": {"egress": "proxy_only"},
    "model_routes": {
        "litellm": {
            "trust": "customer_local",
            "provider": "acme-vllm",
            "data_region": "eu-west-1",
            "training": "no_training",
            "retention_days": 0,
        },
        "claude": {"training": "no_training", "retention_days": 30},
    },
}


@pytest.fixture(autouse=True)
def _no_ambient_document(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DATA_GOVERNANCE_ENV, raising=False)


def _document(tmp_path: Path, body: Any, name: str = "governance.json") -> dict[str, str]:
    path = tmp_path / name
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return {DATA_GOVERNANCE_ENV: str(path)}


def _with_route(**entry: Any) -> dict[str, Any]:
    return {"version": 1, "model_routes": {"litellm": entry}}


# --------------------------------------------------------------------------- #
# parsing: strict, all-or-nothing, never echoed
# --------------------------------------------------------------------------- #


def test_a_full_document_is_read_exactly() -> None:
    from lemoncrow.pro.capabilities.review.data_governance import parse_data_governance

    parsed = parse_data_governance(json.dumps(_FULL))
    assert parsed.state == "attested"
    assert (parsed.storage_region, parsed.egress) == ("eu-west-1", "proxy_only")
    assert parsed.route("litellm") == RouteAttestation("customer_local", "acme-vllm", "eu-west-1", "no_training", 0)
    assert parsed.route("claude") == RouteAttestation(None, "", "", "no_training", 30)
    assert parsed.route("openai") is None


@pytest.mark.parametrize(
    "label",
    [
        "eu-west-1",
        "us-central1",
        "australiacentral2",
        "northamerica-northeast1",
        "azure-openai",
        "-".join(["region"] * 7),
    ],
)
def test_real_region_and_provider_names_are_labels(label: str) -> None:
    from lemoncrow.pro.capabilities.review.data_governance import parse_data_governance

    document = {"version": 1, "storage": {"region": label}, "model_routes": {"openai": {"provider": label}}}
    parsed = parse_data_governance(json.dumps(document))
    assert parsed.storage_region == label and parsed.route("openai") == RouteAttestation(provider=label)


# Every identifier a projection shows passes an allowlist shape: anything that
# could carry a credential, host, address or port is withheld, and an odd but
# legitimate id is withheld rather than risk a leak.
_WITHHELD_MODEL_IDS = [
    "user:hunter2@gw/llama3",
    "admin:Sup3rS3cret@gw/llama3",
    "llama3@[fd00::1]:11434",
    "fd00::1",
    "my_gw.corp/llama3",
    "host.example.com./m",
    "host.example.com",
    "gw1.corp2",
    "gpt-4o@gw.corp1",
    "anthropic/claude-3-5-sonnet@20240620",
    "internal-gw:8443/llama",
    "llama3:11434",
    "0x0a000005/llama",
    "0x0a000005",
    "167772165/llama",
    "167772165",
    "10.0.0.5",
    "ollama/10.0.0.5",
    "llama+gw.corp.example",
    "localhost/llama3",
    "ollama/localhost",
    "https://gw.example.com/v1",
    "openrouter/anthropic/claude-3",
    "acme-gw/llama3",
    "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
    "s3cr3t-p4ss-w0rd-x9y8",
    "passwordpassword",
    "sk-proj-abcdefABCDEF1234567890",
    "abcdefghijklmnopqrstuvwxyzabcdef",
    "tok_9f8e7d6c5b4a",
    "eyJhbGciOiJIUzI1NiJ9",
    "",
]
_SHOWN_MODEL_IDS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1-2025-04-14",
    "gpt-3.5-turbo",
    "o3-mini",
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
    "claude-3-5-sonnet-latest",
    "llama3",
    "llama3:8b",
    "llama3.1:70b",
    "qwen2.5-coder:1.5b",
    "qwen3",
    "mixtral-8x7b",
    "deepseek-r1:70b",
    "ollama/llama3:latest",
    "openai/gpt-4o",
    "anthropic/claude-sonnet-4-5",
    "Qwen/Qwen2.5-Coder-32B-Instruct",
]


@pytest.mark.parametrize("value", _WITHHELD_MODEL_IDS)
def test_a_model_id_that_could_carry_a_secret_host_or_port_is_withheld(value: str) -> None:
    from lemoncrow.pro.capabilities.review.data_governance import plain_name, shown_model_name

    assert shown_model_name(value) == ""
    assert plain_name(value) == ""


@pytest.mark.parametrize("value", _SHOWN_MODEL_IDS)
def test_a_legitimate_model_id_is_still_shown(value: str) -> None:
    from lemoncrow.pro.capabilities.review.data_governance import shown_model_name

    assert shown_model_name(value) == value


@pytest.mark.parametrize(
    "value", ["s3cr3t-p4ss-w0rd-x9y8", "passwordpassword", "my-api-key", "hunter2hunter2", "a1b2c3", "x" * 24]
)
def test_credential_shapes_are_refused_as_names_and_labels(value: str) -> None:
    from lemoncrow.pro.capabilities.review.data_governance import parse_data_governance, plain_name

    assert plain_name(value) == ""
    document = {"version": 1, "model_routes": {"openai": {"provider": value}}}
    with pytest.raises(ValueError):
        parse_data_governance(json.dumps(document))


@pytest.mark.parametrize("value", ["svc_review", "worker-1", "route-a", "eu-west-1", "build_runner_02"])
def test_plain_worker_and_route_names_are_shown(value: str) -> None:
    from lemoncrow.pro.capabilities.review.data_governance import plain_name

    assert plain_name(value) == value


def test_an_unset_setting_attests_nothing() -> None:
    assert load_data_governance({}) is NOT_CONFIGURED
    assert load_data_governance({DATA_GOVERNANCE_ENV: "  "}) is NOT_CONFIGURED


_REFUSED: dict[str, Any] = {
    "unknown top-level key": {"version": 1, CANARY: {}},
    "unsupported version": {"version": 2},
    "boolean version": {"version": True},
    "float version": {"version": 1.0},
    "missing version": {"storage": {"region": "eu-west-1"}},
    "top-level array": [CANARY],
    "unknown driver": {"version": 1, "model_routes": {CANARY: {"training": "no_training"}}},
    "openai_compatible is not a key": {"version": 1, "model_routes": {"openai_compatible": {}}},
    "unknown route key": _with_route(endpoint=f"https://{CANARY}.example.com"),
    "trust outside the vocabulary": _with_route(trust="unknown"),
    "trust of an agent CLI": {"version": 1, "model_routes": {"claude": {"trust": "customer_local"}}},
    "training outside the vocabulary": _with_route(training=CANARY),
    "boolean retention": _with_route(retention_days=True),
    "negative retention": _with_route(retention_days=-1),
    "retention past the bound": _with_route(retention_days=36501),
    "fractional retention": _with_route(retention_days=1.5),
    "string retention": _with_route(retention_days="30"),
    "provider shaped like a host": _with_route(provider=f"{CANARY}.example.com"),
    "provider shaped like a URL": _with_route(provider=f"https://user:{CANARY}@h/v1"),
    "provider shaped like a credential": _with_route(provider=f"sk-{CANARY.upper()}"),
    "provider shaped like a lowercase api key": _with_route(provider=f"sk-{CANARY}"),
    "provider shaped like a hex token": _with_route(provider="0f3a9c41e27b5d6c8a9b0c1d2e3f4a5b"),
    "region shaped like an access key id": {"version": 1, "storage": {"region": "akia2e0a8f3b244c9986"}},
    "provider too long": _with_route(provider="a" * 49),
    "region with a colon": {"version": 1, "storage": {"region": f"{CANARY}:443"}},
    "egress outside the vocabulary": {"version": 1, "network": {"egress": CANARY}},
    "storage not an object": {"version": 1, "storage": CANARY},
    "route not an object": {"version": 1, "model_routes": {"litellm": CANARY}},
}


@pytest.mark.parametrize("name", sorted(_REFUSED))
def test_a_document_with_anything_unexpected_attests_nothing_and_is_never_echoed(tmp_path: Path, name: str) -> None:
    attestations = load_data_governance(_document(tmp_path, _REFUSED[name], f"{CANARY}.json"))
    assert attestations.state == "unreadable", name
    assert (attestations.storage_region, attestations.egress, attestations.routes) == ("", None, ())
    assert attestations.reason == f"{DATA_GOVERNANCE_ENV} " + _WITHHELD.format(kind="ValueError")
    assert CANARY not in attestations.reason and str(tmp_path) not in attestations.reason


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (f'{{"version": 1, "version": 1, "{CANARY}": 0}}', "ValueError"),
        (f"{{not json {CANARY}", "JSONDecodeError"),
        (b'{"version": 1, "storage": {"region": "\xff\xfe"}}', "UnicodeDecodeError"),
        ("[" * 30_000 + "]" * 30_000, "RecursionError"),
        (json.dumps({"version": 1, "storage": {"region": "eu"}}) + " " * MAX_DOCUMENT_BYTES, "ValueError"),
    ],
    ids=["duplicate key", "not json", "not utf-8", "nesting bomb", "one byte past the bound"],
)
def test_an_unparseable_document_is_unreadable_with_only_its_exception_class(
    tmp_path: Path, body: Any, kind: str
) -> None:
    attestations = load_data_governance(_document(tmp_path, body))
    assert attestations.state == "unreadable"
    assert attestations.reason == f"{DATA_GOVERNANCE_ENV} " + _WITHHELD.format(kind=kind)


def test_a_document_at_the_size_bound_is_read(tmp_path: Path) -> None:
    body = json.dumps({"version": 1, "storage": {"region": "eu"}})
    attestations = load_data_governance(_document(tmp_path, body + " " * (MAX_DOCUMENT_BYTES - len(body))))
    assert (attestations.state, attestations.storage_region) == ("attested", "eu")


def test_a_missing_document_never_names_its_path(tmp_path: Path) -> None:
    attestations = load_data_governance({DATA_GOVERNANCE_ENV: str(tmp_path / CANARY / "governance.json")})
    assert attestations.state == "unreadable"
    assert attestations.reason == f"{DATA_GOVERNANCE_ENV} " + _WITHHELD.format(kind="FileNotFoundError")


def test_an_edited_document_is_reread(tmp_path: Path) -> None:
    environ = _document(tmp_path, {"version": 1, "storage": {"region": "eu"}})
    assert load_data_governance(environ).storage_region == "eu"
    Path(environ[DATA_GOVERNANCE_ENV]).write_text(json.dumps({"version": 1, "storage": {"region": "us-east"}}))
    assert load_data_governance(environ).storage_region == "us-east"
    Path(environ[DATA_GOVERNANCE_ENV]).write_text("{broken")
    assert load_data_governance(environ).state == "unreadable"


def test_the_governance_digest_binds_the_effective_document_never_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Job currentness binds this digest: a worker reading another document refuses the job."""

    first = data_governance_digest(load_data_governance(_document(tmp_path, _FULL, "a.json")))
    # The same attestations under another path and formatting are the same authority.
    moved = data_governance_digest(
        load_data_governance(_document(tmp_path, json.dumps(_FULL, indent=2), f"{CANARY}.json"))
    )
    downgraded = data_governance_digest(
        load_data_governance(_document(tmp_path, {**_FULL, "model_routes": {"litellm": {"trust": "external"}}}, "c"))
    )
    broken = data_governance_digest(load_data_governance(_document(tmp_path, "{broken", "d.json")))
    missing = data_governance_digest(load_data_governance({DATA_GOVERNANCE_ENV: str(tmp_path / "missing.json")}))
    unset = data_governance_digest(NOT_CONFIGURED)
    assert first == moved and CANARY not in first
    # Both unreadable documents attest nothing: the same (fail-closed) authority.
    assert broken == missing
    assert len({first, downgraded, broken, unset}) == 4
    # Without an argument it is this process's document.
    monkeypatch.setenv(DATA_GOVERNANCE_ENV, str(tmp_path / "a.json"))
    assert data_governance_digest() == first
    monkeypatch.delenv(DATA_GOVERNANCE_ENV)
    assert data_governance_digest() == unset


# --------------------------------------------------------------------------- #
# the route projection
# --------------------------------------------------------------------------- #


def test_without_attestations_only_the_driver_classifier_decides_and_litellm_stays_unknown() -> None:
    trusts = {
        driver: (route_governance(driver, attestations=NOT_CONFIGURED).trust)
        for driver in ("litellm", "ollama", "openai", "openai_compatible", "claude", CANARY)
    }
    assert trusts == {
        "litellm": "unknown",
        # A driver name never proves where its endpoint runs.
        "ollama": "unknown",
        "openai": "external",
        "openai_compatible": "external",
        "claude": "external",
        CANARY: "unknown",
    }
    governed = route_governance("openai", attestations=NOT_CONFIGURED)
    assert governed.to_wire() == {
        "driver": "openai",
        "trust": "external",
        "data_locus": "external",
        "trust_source": "driver_classifier",
        "provider": "unknown",
        "data_region": "unknown",
        "training": "unknown",
        "retention_days": "unknown",
    }
    assert not governed.terms_known


def _attested(**routes: RouteAttestation) -> DataGovernanceAttestations:
    return DataGovernanceAttestations("attested", routes=tuple(sorted(routes.items())))


def test_an_attested_trust_decides_including_a_downgrade() -> None:
    attestations = _attested(
        litellm=RouteAttestation(trust="customer_local"),
        openai=RouteAttestation(trust="customer_local", provider="self-hosted"),
        ollama=RouteAttestation(trust="external"),
        claude=RouteAttestation(training="may_train", retention_days=30),
    )
    governed = {d: route_governance(d, attestations=attestations) for d in ("litellm", "openai_compatible", "ollama")}
    assert {d: (g.trust, g.trust_source) for d, g in governed.items()} == {
        "litellm": ("customer_local", "operator_attested"),
        "openai_compatible": ("customer_local", "operator_attested"),
        "ollama": ("external", "operator_attested"),
    }
    claude = route_governance("claude", attestations=attestations)
    assert (claude.trust, claude.trust_source, claude.terms_known) == ("external", "driver_classifier", True)
    assert claude.to_wire()["retention_days"] == 30 and claude.to_wire()["training"] == "may_train"


def test_an_unreadable_document_makes_every_attestable_route_unknown() -> None:
    unreadable = DataGovernanceAttestations("unreadable", reason="r")
    for driver in ("litellm", "openai", "ollama"):
        governed = route_governance(driver, attestations=unreadable)
        assert (governed.trust, governed.trust_source) == ("unknown", "attestation_unreadable"), driver
    # Agent CLIs are always external: an unreadable document cannot make them less known.
    assert route_governance("codex", attestations=unreadable).trust == "external"


def test_process_route_trust_reads_the_configured_document_and_an_explicit_trust_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert process_route_trust("litellm") == "unknown"
    monkeypatch.setenv(DATA_GOVERNANCE_ENV, _document(tmp_path, _FULL)[DATA_GOVERNANCE_ENV])
    assert process_route_trust("litellm") == "customer_local"
    # A route trust recorded elsewhere (a customer worker route policy) is never overridden.
    assert process_route_trust("litellm", explicit="external") == "external"
    assert process_route_trust("openai", explicit="customer_local") == "customer_local"


@pytest.mark.parametrize(
    ("document", "driver", "allowed"),
    [
        (None, "litellm", False),
        ({"version": 1}, "litellm", False),
        (_with_route(trust="customer_local"), "litellm", True),
        (_with_route(trust="external"), "litellm", False),
        ({"version": 1, "model_routes": {"ollama": {"trust": "external"}}}, "ollama", False),
        (None, "ollama", False),
        ({"version": 1, "model_routes": {"ollama": {"trust": "customer_local"}}}, "ollama", True),
        ("{broken", "ollama", False),
        ("{broken", "litellm", False),
    ],
    ids=[
        "litellm unattested",
        "litellm silent document",
        "litellm attested customer_local",
        "litellm attested external",
        "ollama attested external",
        "ollama unattested",
        "ollama attested customer_local",
        "unreadable ollama",
        "unreadable litellm",
    ],
)
def test_customer_local_only_content_follows_the_attested_route_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: Any, driver: str, allowed: bool
) -> None:
    if document is not None:
        monkeypatch.setenv(DATA_GOVERNANCE_ENV, _document(tmp_path, document)[DATA_GOVERNANCE_ENV])
    rule = ContentAccessRule(
        mode="customer_local_only",
        path_scope=("secrets/**",),
        origin="trusted base",
        trust_class="trusted_base_repository",
        source_identity="base",
    )
    decision = content_access_for_path("secrets/key.txt", policy=(rule,), route_trust=process_route_trust(driver))
    assert decision.allowed is allowed
