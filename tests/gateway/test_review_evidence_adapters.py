"""Trust T4: the external evidence adapter SPI and the in-toto/DSSE adapter (trust plan section 31).

Acceptance: two adapters producing equivalent display names cannot impersonate
each other's authority. Authority comes from the server's grant and from what
it authenticated or verified, never from the payload.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lemoncrow.pro.capabilities.review import check_ingest_api
from lemoncrow.pro.capabilities.review.check_ingest import (
    CheckIngest,
    IngestRefused,
    load_ingest,
    parse_ci_equivalents,
    record_check_ingest,
)
from lemoncrow.pro.capabilities.review.evidence_adapters import (
    IN_TOTO_PAYLOAD_TYPE,
    TEST_RESULT_PREDICATE,
    AuthorityClaim,
    EvidenceChannel,
    InTotoAdapter,
    ProviderCheckAdapter,
    ResultScope,
    ReviewApiAdapter,
    admit,
    attested_app_id,
    authorize,
    key_thumbprint,
    provider_check_authority,
    trusted_attestation_keys,
)
from lemoncrow.pro.capabilities.review.record_signing import pae
from lemoncrow.pro.capabilities.review.record_verify import _signature_valid, parse_key
from lemoncrow.pro.capabilities.review.verification import check_key
from tests.gateway.test_review_check_ingest import _entry, _get, _latest, _post, _push, _review

TITLE = "CI / unit tests"
CONFIG_URI = "https://github.com/acme/app/blob/main/.github/workflows/test.yml"


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


# --------------------------------------------------------------------------- #
# keys and envelopes
# --------------------------------------------------------------------------- #


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class _Signer:
    def __init__(self, kid: str, algorithm: str = "Ed25519", **lifecycle: str) -> None:
        self.kid = kid
        self.private: Any
        if algorithm == "Ed25519":
            self.private = Ed25519PrivateKey.generate()
            raw = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            self.jwk: dict[str, str] = {"kid": kid, "kty": "OKP", "crv": "Ed25519", "x": _b64url(raw)}
        else:
            self.private = ec.generate_private_key(ec.SECP256R1())
            numbers = self.private.public_key().public_numbers()
            self.jwk = {
                "kid": kid,
                "kty": "EC",
                "crv": "P-256",
                "x": _b64url(numbers.x.to_bytes(32, "big")),
                "y": _b64url(numbers.y.to_bytes(32, "big")),
            }
        self.jwk.update(lifecycle)
        self.algorithm = algorithm

    @property
    def thumbprint(self) -> str:
        return key_thumbprint(parse_key(self.jwk))

    def sign(self, message: bytes) -> bytes:
        if self.algorithm == "Ed25519":
            signature: bytes = self.private.sign(message)
        else:
            signature = self.private.sign(message, ec.ECDSA(hashes.SHA256()))
        return signature


def _statement(
    head: str,
    *,
    result: str = "PASSED",
    uri: str = CONFIG_URI,
    predicate_type: str = TEST_RESULT_PREDICATE,
    subjects: Sequence[Mapping[str, Any]] | None = None,
    configuration: Any = None,
) -> dict[str, Any]:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": list(subjects) if subjects is not None else [{"name": "commit", "digest": {"gitCommit": head}}],
        "predicateType": predicate_type,
        "predicate": {
            "result": result,
            "configuration": (
                configuration if configuration is not None else [{"uri": uri, "digest": {"sha256": "ab" * 32}}]
            ),
            "url": "https://ci.example.invalid/runs/1",
            "passedTests": ["test_ok"],
            "warnedTests": [],
            "failedTests": ["test_bad"] if result == "FAILED" else [],
        },
    }


def _envelope(
    statement: Mapping[str, Any],
    *signers: _Signer,
    payload_type: str = IN_TOTO_PAYLOAD_TYPE,
    keyid: str | None = None,
    payload: bytes | None = None,
) -> dict[str, Any]:
    signed = json.dumps(statement).encode("utf-8")
    message = pae(payload_type, signed)
    return {
        "payloadType": payload_type,
        "payload": base64.b64encode(signed if payload is None else payload).decode("ascii"),
        "signatures": [
            {"keyid": signer.kid if keyid is None else keyid, "sig": base64.b64encode(signer.sign(message)).decode()}
            for signer in signers
        ],
    }


def _keys(*signers: _Signer) -> dict[str, Any]:
    keys, errors = trusted_attestation_keys(_keys_yaml(*signers))
    assert errors == ()
    return keys


def _keys_yaml(*signers: _Signer) -> str:
    return "attestation_keys:\n" + "".join(f"  - {json.dumps(signer.jwk)}\n" for signer in signers)


def _channel(keys: Mapping[str, Any] | None = None, *, source: str = "ci") -> EvidenceChannel:
    return EvidenceChannel(kind="review_api", source=source, actor="reviewer", trusted_keys=dict(keys or {}))  # type: ignore[arg-type]


def _admit_in_toto(envelope: Mapping[str, Any], keys: Mapping[str, Any], **body: Any) -> tuple[Any, Any]:
    return admit(InTotoAdapter(), {"format": "in_toto", "envelope": envelope, **body}, _channel(keys))


# --------------------------------------------------------------------------- #
# the SPI: grants
# --------------------------------------------------------------------------- #


class _Claiming(ReviewApiAdapter):
    """An adapter that claims whatever authority it is told to."""

    def __init__(self, adapter_id: str, claim: AuthorityClaim) -> None:
        self.adapter_id = adapter_id
        self._claim = claim

    def verify_authority(self, raw: Mapping[str, Any], channel: EvidenceChannel) -> AuthorityClaim:
        return self._claim


_PLAIN = {"head_sha": "a" * 40, "title": TITLE, "stable_key": "unit-tests", "status": "PASS"}


def test_an_adapter_produces_only_what_its_server_side_grant_allows() -> None:
    provider = AuthorityClaim("provider_ci", provider="github", integration_id="actions")
    # The Review API adapter can never produce provider CI, however it asks.
    with pytest.raises(IngestRefused, match="may not produce provider_ci"):
        admit(_Claiming("review_api", provider), _PLAIN, _channel())
    with pytest.raises(IngestRefused, match="not registered"):
        admit(_Claiming("my_ci", provider), _PLAIN, _channel())
    # The provider adapter is accepted only on the authenticated provider channel ...
    with pytest.raises(IngestRefused, match="not accepted on the review_api channel"):
        admit(_Claiming("provider_checks", provider), _PLAIN, _channel())
    webhook = EvidenceChannel(kind="provider_webhook", source="external", provider="github", integration_id="actions")
    _ingest, authority = admit(_Claiming("provider_checks", provider), _PLAIN, webhook)
    assert (authority.attestation_class, authority.provider, authority.integration_id) == (
        "provider_ci",
        "github",
        "actions",
    )
    # ... and only for the integration that channel authenticated.
    impostor = AuthorityClaim("provider_ci", provider="github", integration_id="codeql")
    with pytest.raises(IngestRefused, match="authenticated provider and integration"):
        admit(_Claiming("provider_checks", impostor), _PLAIN, webhook)
    with pytest.raises(IngestRefused, match="authenticated provider and integration"):
        admit(_Claiming("provider_checks", provider), _PLAIN, replace(webhook, integration_id=""))
    # An unauthenticated channel and an empty claim agree on nothing; that is no authority.
    with pytest.raises(IngestRefused, match="needs an authenticated provider"):
        admit(
            _Claiming("provider_checks", AuthorityClaim("provider_ci")),
            _PLAIN,
            replace(webhook, provider="", integration_id=""),
        )
    # A signed claim needs a key the trusted base names; a human claim needs a human.
    signed = AuthorityClaim("signed_attestation", provider="in-toto", integration_id="made-up")
    with pytest.raises(IngestRefused, match="key the trusted base names"):
        admit(_Claiming("in_toto", signed), _PLAIN, _channel())
    signer = _Signer("release-ci")
    relabelled = AuthorityClaim("signed_attestation", provider="github", integration_id=signer.thumbprint)
    with pytest.raises(IngestRefused, match="key the trusted base names"):
        admit(_Claiming("in_toto", relabelled), _PLAIN, _channel(_keys(signer)))
    with pytest.raises(IngestRefused, match="only a human"):
        admit(_Claiming("review_api", AuthorityClaim("human_observation")), _PLAIN, _channel(source="ci"))


def test_the_provider_adapter_mints_provider_ci_only_on_the_provider_channel() -> None:
    authority = provider_check_authority("github", "ci", "9")
    assert (authority.attestation_class, authority.source, authority.provider, authority.integration_id) == (
        "provider_ci",
        "external",
        "github",
        "ci",
    )
    assert authority.attested_by == "github:ci@app:9" and attested_app_id(authority.attested_by) == "9"
    assert provider_check_authority("github", "commit-status").attested_by == "github:commit-status"
    assert attested_app_id("github:commit-status") == "" and attested_app_id("reviewer") == ""
    with pytest.raises(IngestRefused, match="needs an authenticated provider"):
        provider_check_authority("github", "")
    with pytest.raises(IngestRefused, match="not accepted on the review_api channel"):
        authorize(ProviderCheckAdapter(), {"integration": "ci"}, _channel())
    # The Review API route never reaches the provider adapter.
    route = Path(check_ingest_api.__file__).read_text(encoding="utf-8")
    assert "ProviderCheckAdapter" not in route and "provider_check_authority" not in route


def test_review_api_posts_are_caller_attested_whatever_the_body_claims() -> None:
    body = {**_PLAIN, "attestation_class": "provider_ci", "provider": "github", "integration_id": "actions"}
    _ingest, human = admit(ReviewApiAdapter(), body, _channel(source="human"))
    assert (human.attestation_class, human.provider, human.attested_by) == ("human_observation", "", "reviewer")
    _ingest, reported = admit(ReviewApiAdapter(), body, _channel(source="ci"))
    assert (reported.attestation_class, reported.provider, reported.typed) == ("", "", False)
    # Even the in-toto adapter cannot be talked into a class by the body.
    with pytest.raises(IngestRefused, match="envelope"):
        admit(InTotoAdapter(), {**body, "format": "in_toto"}, _channel(source="ci"))


def test_the_scope_and_subject_come_from_the_adapter_not_the_normalized_result() -> None:
    ingest, _authority = admit(
        ReviewApiAdapter(), {**_PLAIN, "config_digest": "sha256:cfg", "recipe": "checks.test"}, _channel()
    )
    assert (ingest.head_sha, ingest.config_digest, ingest.recipe) == ("a" * 40, "sha256:cfg", "checks.test")
    assert ReviewApiAdapter().normalize_scope({"recipe_digest": "r" * 200}) == ResultScope(recipe_digest="r" * 128)


# --------------------------------------------------------------------------- #
# in-toto / DSSE
# --------------------------------------------------------------------------- #


def test_the_thumbprint_is_rfc_8037s_and_ignores_the_label_and_encoding() -> None:
    # RFC 8037 appendix A.3.
    jwk = {"kid": "a", "kty": "OKP", "crv": "Ed25519", "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}
    assert key_thumbprint(parse_key(jwk)) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
    padded = {**jwk, "kid": "another-label", "x": jwk["x"] + "="}
    assert key_thumbprint(parse_key(padded)) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"


def test_trusted_keys_are_parsed_strictly_and_one_key_is_one_identity() -> None:
    signer = _Signer("release-ci")
    again = {**signer.jwk, "kid": "other-label"}
    content = _keys_yaml(signer) + f"  - {json.dumps(again)}\n  - {{kid: bad, kty: RSA}}\n  - not-a-key\n"
    keys, errors = trusted_attestation_keys(content)
    assert list(keys) == [signer.thumbprint]
    assert [error.split(" ")[0] for error in errors] == [
        "attestation_keys[1]",
        "attestation_keys[2]:",
        "attestation_keys[3]",
    ]
    assert trusted_attestation_keys("attestation_keys: nope\n") == ({}, ("attestation_keys must be a list of JWKs",))
    assert trusted_attestation_keys("") == ({}, ())


@pytest.mark.parametrize("algorithm", ["Ed25519", "ES256"])
def test_a_verified_test_result_is_typed_by_its_key_and_signed_configuration(algorithm: str) -> None:
    head = "c" * 40
    signer = _Signer("release-ci", algorithm)
    # Only presentation comes from the body: identity, status, subject and scope are the signed Statement's.
    spoof = {"stable_key": "spoof", "status": "FAIL", "head_sha": "d" * 40, "config_digest": "sha256:spoof"}
    ingest, authority = _admit_in_toto(_envelope(_statement(head), signer), _keys(signer), title="Shown title", **spoof)
    assert (authority.attestation_class, authority.provider, authority.integration_id, authority.source) == (
        "signed_attestation",
        "in-toto",
        signer.thumbprint,
        "external",
    )
    assert authority.attested_by == "in-toto:release-ci"
    assert (ingest.head_sha, ingest.status, ingest.stable_key, ingest.title) == (
        head,
        "PASS",
        CONFIG_URI,
        "Shown title",
    )
    assert ingest.config_digest == "sha256:" + "ab" * 32
    assert ingest.remote_identity.startswith("sha256:") and ingest.format == "in_toto"
    kept = json.loads(ingest.attestation)
    assert kept["key_thumbprint"] == signer.thumbprint and kept["envelope"]["payloadType"] == IN_TOTO_PAYLOAD_TYPE
    failed, _ = _admit_in_toto(_envelope(_statement(head, result="FAILED"), signer), _keys(signer))
    warned, _ = _admit_in_toto(_envelope(_statement(head, result="WARNED"), signer), _keys(signer))
    assert (failed.status, warned.status) == ("FAIL", "UNKNOWN")
    assert "1 failed" in failed.status_basis


def test_only_a_signature_by_an_active_trusted_key_over_the_exact_payload_verifies() -> None:
    head = "c" * 40
    trusted, stranger = _Signer("release-ci"), _Signer("stranger")
    keys = _keys(trusted)
    refused = {
        "unknown key": _envelope(_statement(head), stranger),
        "altered payload": _envelope(
            _statement(head), trusted, payload=json.dumps(_statement(head, result="FAILED")).encode()
        ),
        "wrong payload type": _envelope(_statement(head), trusted, payload_type="application/json"),
        "no signature": {**_envelope(_statement(head), trusted), "signatures": []},
        "too many signatures": _envelope(_statement(head), *([trusted] * 9)),
    }
    for name, envelope in refused.items():
        with pytest.raises(IngestRefused):
            _admit_in_toto(envelope, keys)
        assert name
    with pytest.raises(IngestRefused, match="names no attestation_keys"):
        _admit_in_toto(_envelope(_statement(head), trusted), {})
    # The stored envelope must re-verify as it reads: a relabelled payloadType is refused even if the signature holds.
    with pytest.raises(IngestRefused, match="payloadType"):
        _admit_in_toto({**_envelope(_statement(head), trusted), "payloadType": "application/json"}, keys)
    # A second, valid signature by a trusted key verifies even beside a stranger's.
    _ingest, authority = _admit_in_toto(_envelope(_statement(head), stranger, trusted), keys)
    assert authority.integration_id == trusted.thumbprint


def test_a_revoked_expired_or_not_yet_valid_key_verifies_nothing() -> None:
    head = "c" * 40
    now = datetime.now(UTC)
    past, future = (now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat()
    for lifecycle in ({"status": "revoked"}, {"revoked_at": past}, {"not_after": past}, {"not_before": future}):
        signer = _Signer("release-ci", **lifecycle)
        with pytest.raises(IngestRefused, match="active key"):
            _admit_in_toto(_envelope(_statement(head), signer), _keys(signer))
    later = _Signer("release-ci", revoked_at=future, not_before=past, not_after=future)
    _ingest, authority = _admit_in_toto(_envelope(_statement(head), later), _keys(later))
    assert authority.integration_id == later.thumbprint


def test_only_the_object_that_was_verified_is_normalized() -> None:
    head = "c" * 40
    signer = _Signer("release-ci")
    verified = {"format": "in_toto", "envelope": _envelope(_statement(head), signer)}
    adapter = InTotoAdapter()
    with pytest.raises(IngestRefused, match="not verified"):
        adapter.normalize_result(verified, snapshot_paths=())
    adapter.verify_authority(verified, _channel(_keys(signer)))
    # An equal-looking but different (unverified) object is never read under the verified authority.
    swapped = {"format": "in_toto", "envelope": _envelope(_statement(head, result="FAILED"), _Signer("x"))}
    with pytest.raises(IngestRefused, match="not verified"):
        adapter.normalize_result(swapped, snapshot_paths=())
    with pytest.raises(IngestRefused, match="not verified"):
        adapter.identify_subject(dict(verified))
    assert adapter.normalize_result(verified, snapshot_paths=()).status == "PASS"


def test_the_unsigned_keyid_hint_cannot_relabel_whose_attestation_it_is() -> None:
    head = "c" * 40
    first, second = _Signer("unit"), _Signer("lint")
    envelope = _envelope(_statement(head), first, keyid="lint")
    _ingest, authority = _admit_in_toto(envelope, _keys(first, second))
    assert authority.integration_id == first.thumbprint and authority.attested_by == "in-toto:unit"


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ({"_type": "https://example.invalid/Statement"}, "not an in-toto Statement"),
        (_statement("c" * 40, predicate_type="https://slsa.dev/provenance/v1"), "only the"),
        (_statement("c" * 40, configuration=[]), "names no configuration"),
        (_statement("c" * 40, configuration=[{"digest": {}}]), "no uri or name"),
        (_statement("c" * 40, result="SKIPPED"), "PASSED, WARNED or FAILED"),
        (
            _statement(
                "c" * 40,
                subjects=[{"digest": {"gitCommit": "c" * 40}}, {"digest": {"gitCommit": "d" * 40}}],
            ),
            "exactly one gitCommit",
        ),
        (_statement("c" * 40, subjects=[{"digest": {"sha256": "c" * 64}}]), "exactly one gitCommit"),
        (_statement("not-hex"), "hexadecimal commit"),
    ],
)
def test_only_a_test_result_statement_about_one_commit_is_read(statement: dict[str, Any], message: str) -> None:
    signer = _Signer("release-ci")
    with pytest.raises(IngestRefused, match=message):
        _admit_in_toto(_envelope(statement, signer), _keys(signer))


# --------------------------------------------------------------------------- #
# acceptance: equal display names, separate authority
# --------------------------------------------------------------------------- #


def _equivalents(*rows: str) -> str:
    return "review:\n  required: [checks.test]\nci_equivalents:\n" + "".join(rows)


def _signed_row(signer: _Signer) -> str:
    return (
        f"  - recipe: checks.test\n    check: {CONFIG_URI}\n    provider: in-toto\n"
        f"    attestation_class: signed_attestation\n    integration: {signer.thumbprint}\n"
    )


def _post_in_toto(review: Any, envelope: Mapping[str, Any], **body: Any) -> Any:
    return _post(review, "checks/ingest", {"format": "in_toto", "source": "ci", "envelope": envelope, **body})


def test_equal_display_names_never_share_authority(tmp_path: Path) -> None:
    """Trust plan section 31 acceptance, across every adapter on one Snapshot."""

    pinned, other = _Signer("release-ci"), _Signer("another-ci")
    config = _keys_yaml(pinned, other) + _equivalents(_signed_row(pinned))
    review = _review(tmp_path, base_config=config)
    revision = _latest(review)
    head = revision.head_sha

    # The pinned key's run failed; every impostor below passes under the same title and check name.
    failed = _post_in_toto(review, _envelope(_statement(head, result="FAILED"), pinned), title=TITLE)
    assert failed.status_code == 201, failed.text
    assert failed.json()["authority"] == "attested"
    rows = [
        _post_in_toto(review, _envelope(_statement(head), other), title=TITLE),
        _post(
            review,
            "checks/ingest",
            {
                "source": "ci",
                "head_sha": head,
                "title": TITLE,
                "status": "PASS",
                "stable_key": CONFIG_URI,
                "tool": "in-toto test-result",
            },
        ),
        _post(
            review,
            "checks/ingest",
            {"source": "human", "head_sha": head, "title": TITLE, "status": "PASS", "stable_key": CONFIG_URI},
        ),
    ]
    assert [row.status_code for row in rows] == [201, 201, 201], [row.text for row in rows]
    _ingest, provider = admit(
        _Claiming("provider_checks", AuthorityClaim("provider_ci", provider="github", integration_id="actions")),
        _PLAIN,
        EvidenceChannel(kind="provider_webhook", source="external", provider="github", integration_id="actions"),
    )
    record_check_ingest(
        review.store,
        revision,
        CheckIngest(tool="pytest", category="tests", status="PASS", head_sha=head, title=TITLE, stable_key=CONFIG_URI),
        provider,
    )

    stored = [row for row in review.store.list_evidence(review.review_id) if row.title == TITLE]
    assert len(stored) == 5
    identities = {check_key(row) for row in stored} - {""}
    assert len(identities) == 3  # two keys and one provider integration; caller-attested rows have none
    entry = _entry(_get(review, "run-plan")["plan"], "checks.test")
    assert entry["status"] == "satisfied"
    assert entry["satisfied_by"]["evidence_id"] == failed.json()["evidence"]["id"]
    assert entry["satisfied_by"]["status"] == "FAIL"
    verification = [row for row in _get(review, "verification")["checks"] if row["name"] == TITLE]
    assert sorted(row["authority"] for row in verification) == ["attested", "attested", "attested", "human", "reported"]


def test_a_signed_result_is_stored_re_verifiable_and_bound_to_its_exact_snapshot(tmp_path: Path) -> None:
    signer = _Signer("release-ci", "ES256")
    review = _review(tmp_path, base_config=_keys_yaml(signer) + _equivalents(_signed_row(signer)))
    head = _latest(review).head_sha
    before = len(review.store.list_evidence(review.review_id))

    # Another commit's result is refused, never re-labelled; an unverifiable one stores nothing.
    elsewhere = _post_in_toto(review, _envelope(_statement("d" * 40), signer))
    assert elsewhere.status_code == 409, elsewhere.text
    stranger = _post_in_toto(review, _envelope(_statement(head), _Signer("stranger")))
    assert stranger.status_code == 422 and "trusted base" in stranger.text
    assert len(review.store.list_evidence(review.review_id)) == before

    posted = _post_in_toto(review, _envelope(_statement(head), signer))
    assert posted.status_code == 201, posted.text
    evidence = posted.json()["evidence"]
    assert (evidence["attestation_class"], evidence["provider"], evidence["integration_id"]) == (
        "signed_attestation",
        "in-toto",
        signer.thumbprint,
    )
    assert _entry(_get(review, "run-plan")["plan"], "checks.test")["status"] == "satisfied"
    row = next(item for item in review.store.list_evidence(review.review_id) if item.id == evidence["id"])
    document = load_ingest(review.store, row)
    assert document is not None and document.format == "in_toto" and document.head_sha == head
    artifact = json.loads(review.store.read_evidence_artifact(row))
    envelope = artifact["attestation"]["envelope"]
    message = pae(envelope["payloadType"], base64.b64decode(envelope["payload"]))
    key = trusted_attestation_keys(_keys_yaml(signer))[0][signer.thumbprint]
    assert _signature_valid(key, base64.b64decode(envelope["signatures"][0]["sig"]), message)
    # A replay is the same observation.
    again = _post_in_toto(review, envelope)
    assert again.status_code == 201 and again.json()["created"] is False


def test_a_key_added_on_the_head_side_never_verifies_its_own_review(tmp_path: Path) -> None:
    signer = _Signer("release-ci")
    review = _review(tmp_path)
    revision = _push(review, ".lemoncrow/review.yaml", _keys_yaml(signer) + _equivalents(_signed_row(signer)))
    refused = _post_in_toto(review, _envelope(_statement(revision.head_sha), signer))
    assert refused.status_code == 422 and "names no attestation_keys" in refused.text


def test_a_signed_equivalent_must_pin_one_key() -> None:
    rows, errors = parse_ci_equivalents(
        _equivalents(
            f"  - recipe: checks.test\n    check: {CONFIG_URI}\n    provider: in-toto\n"
            "    attestation_class: signed_attestation\n",
            "  - recipe: checks.lint\n    check: lint\n    provider: github\n    attestation_class: signed_attestation\n"
            "    integration: abc\n",
        )
    )
    assert rows == ()
    assert all("key thumbprint" in error for error in errors) and len(errors) == 2


# --------------------------------------------------------------------------- #
# replay, premerge pinning, revocation after ingest, duplicate keys
# --------------------------------------------------------------------------- #

UNIT_URI = "https://github.com/acme/app/blob/main/.github/workflows/unit.yml"
LINT_URI = "https://github.com/acme/app/blob/main/.github/workflows/lint.yml"


def _envelope_variants(envelope: Mapping[str, Any], signer: _Signer) -> dict[str, dict[str, Any]]:
    """The same signed payload under envelopes that differ only in unsigned bytes."""

    message = pae(IN_TOTO_PAYLOAD_TYPE, base64.b64decode(envelope["payload"]))
    junk = {"keyid": "junk", "sig": base64.b64encode(b"\x00" * 64).decode()}
    original = dict(envelope["signatures"][0])
    resigned = base64.b64encode(signer.sign(message)).decode()  # ECDSA: new, equally valid bytes
    assert resigned != original["sig"]
    return {
        "keyid": {**envelope, "signatures": [{**original, "keyid": "anything-else"}]},
        "extra top-level key": {**envelope, "junk": "x"},
        "junk signature first": {**envelope, "signatures": [junk, original]},
        "junk signature last": {**envelope, "signatures": [original, junk]},
        "extra signature member": {**envelope, "signatures": [{**original, "note": "relabelled"}]},
        "re-signed (malleable ECDSA bytes)": {**envelope, "signatures": [{"keyid": "", "sig": resigned}]},
    }


def test_a_replayed_older_pass_never_outranks_a_newer_fail_however_its_envelope_is_altered(tmp_path: Path) -> None:
    """Dedupe is on the signed payload and the verifying key, never on the envelope's unsigned bytes."""

    signer = _Signer("release-ci", "ES256")
    review = _review(tmp_path, base_config=_keys_yaml(signer) + _equivalents(_signed_row(signer)))
    head = _latest(review).head_sha
    passed_envelope = _envelope(_statement(head), signer)
    passed = _post_in_toto(review, passed_envelope, title=TITLE)
    failed = _post_in_toto(review, _envelope(_statement(head, result="FAILED"), signer), title=TITLE)
    assert passed.status_code == failed.status_code == 201
    assert passed.json()["created"] is failed.json()["created"] is True

    def current() -> tuple[str, str]:
        entry = _entry(_get(review, "run-plan")["plan"], "checks.test")
        [row] = [
            row for row in _get(review, "verification")["checks"] if row["attestation_class"] == "signed_attestation"
        ]
        assert entry["satisfied_by"]["evidence_id"] == row["evidence_id"]
        return row["evidence_id"], row["status"]

    assert current() == (failed.json()["evidence"]["id"], "FAIL")
    for name, variant in _envelope_variants(passed_envelope, signer).items():
        replay = _post_in_toto(review, variant, title=TITLE + " (replayed)", category="ci")
        assert replay.status_code == 201, (name, replay.text)
        assert replay.json()["created"] is False, name
        assert replay.json()["evidence"]["id"] == passed.json()["evidence"]["id"], name
        assert current() == (failed.json()["evidence"]["id"], "FAIL"), name

    # Only the payload, its type and the one signature that verified are stored.
    row = next(
        item for item in review.store.list_evidence(review.review_id) if item.id == passed.json()["evidence"]["id"]
    )
    stored = json.loads(review.store.read_evidence_artifact(row))["attestation"]["envelope"]
    assert set(stored) == {"payload", "payloadType", "signatures"}
    assert stored["signatures"] == [{"sig": passed_envelope["signatures"][0]["sig"]}]
    assert stored["payload"] == passed_envelope["payload"]

    # A real rerun (a different signed payload) is a newer observation and stands.
    rerun = _statement(head)
    rerun["predicate"]["url"] = "https://ci.example.invalid/runs/2"
    second = _post_in_toto(review, _envelope(rerun, signer), title=TITLE)
    assert second.status_code == 201 and second.json()["created"] is True
    assert current() == (second.json()["evidence"]["id"], "PASS")


def test_a_row_stored_before_signed_dedupe_still_dedupes_its_signed_payload(tmp_path: Path) -> None:
    """A pre-fix signed row (keyed by the whole posted document) is still that observation."""

    signer = _Signer("release-ci")
    review = _review(tmp_path, base_config=_keys_yaml(signer))
    envelope = _envelope(_statement(_latest(review).head_sha), signer)
    first = _post_in_toto(review, envelope, title=TITLE)
    assert first.status_code == 201 and first.json()["created"] is True
    evidence_id = first.json()["evidence"]["id"]
    # The key a pre-fix ingest wrote: a digest of the posted document, not of the signed payload.
    with review.store._transaction() as conn:
        conn.execute("UPDATE review_evidence SET source_ref = ? WHERE id = ?", ("ingest:" + "0" * 32, evidence_id))
    replay = _post_in_toto(review, {**envelope, "signatures": [{**envelope["signatures"][0], "keyid": "x"}]}, title="x")
    assert replay.status_code == 201 and replay.json()["created"] is False
    assert replay.json()["evidence"]["id"] == evidence_id
    assert [row.id for row in review.store.list_evidence(review.review_id)] == [evidence_id]


def test_a_signed_payload_above_the_cap_is_refused_before_it_is_verified() -> None:
    from lemoncrow.pro.capabilities.review.evidence_adapters import MAX_SIGNED_PAYLOAD_BYTES

    signer = _Signer("release-ci")
    statement = _statement("c" * 40)
    statement["predicate"]["passedTests"] = ["t" * 1000] * (MAX_SIGNED_PAYLOAD_BYTES // 1000 + 1)
    with pytest.raises(IngestRefused, match="at most"):
        _admit_in_toto(_envelope(statement, signer), _keys(signer))
    statement["predicate"]["passedTests"] = ["t" * 1000] * (MAX_SIGNED_PAYLOAD_BYTES // 1010)
    _ingest, authority = _admit_in_toto(_envelope(statement, signer), _keys(signer))
    assert authority.integration_id == signer.thumbprint
    # One byte over: its base64 still fits the encoded bound, so the decoded size refuses it.
    statement["predicate"]["passedTests"] = [""]
    statement["predicate"]["passedTests"] = ["t" * (MAX_SIGNED_PAYLOAD_BYTES + 1 - len(json.dumps(statement)))]
    assert len(json.dumps(statement).encode()) == MAX_SIGNED_PAYLOAD_BYTES + 1
    with pytest.raises(IngestRefused, match="at most"):
        _admit_in_toto(_envelope(statement, signer), _keys(signer))
    # An oversized encoded payload is refused before it is decoded.
    oversized = {**_envelope(_statement("c" * 40), signer), "payload": "!" * (MAX_SIGNED_PAYLOAD_BYTES * 2)}
    with pytest.raises(IngestRefused, match="at most"):
        _admit_in_toto(oversized, _keys(signer))


def _premerge_yaml(*, checks: Sequence[str], keys: Sequence[str] | None) -> str:
    pinned = "" if keys is None else f"      key_thumbprints: {json.dumps(list(keys))}\n"
    return (
        "premerge:\n  checks:\n    - id: required-verification\n"
        f"      checks: {json.dumps(list(checks))}\n      attestation: [signed_attestation]\n" + pinned
    )


def test_a_signed_premerge_requirement_must_pin_a_key() -> None:
    from lemoncrow.pro.capabilities.review.premerge_checks import parse_premerge_section

    def errors(**check: Any) -> tuple[str, ...]:
        section = {"checks": [{"id": "required-verification", "checks": [UNIT_URI], **check}]}
        return parse_premerge_section(section, layer="trusted_base", source="base review.yaml").errors

    assert errors(attestation=["provider_ci"]) == ()
    assert errors(attestation=["signed_attestation"], key_thumbprints=["thumb"]) == ()
    (unpinned,) = errors(attestation=["signed_attestation"])
    assert "key_thumbprints" in unpinned and "signed_attestation" in unpinned
    (stray,) = errors(attestation=["provider_ci"], key_thumbprints=["thumb"])
    assert "key_thumbprints" in stray


def test_premerge_counts_a_signed_result_only_by_its_signed_identity_and_pinned_key(tmp_path: Path) -> None:
    """A relabelled title or another trusted key never stands in for the pinned key's signed check."""

    unit, lint = _Signer("unit-ci"), _Signer("lint-ci")
    config = _keys_yaml(unit, lint) + _premerge_yaml(checks=["unit-tests", UNIT_URI], keys=[unit.thumbprint])
    review = _review(tmp_path, base_config=config)
    head = _latest(review).head_sha

    def premerge() -> tuple[str, str]:
        body = _get(review, "premerge")
        assert body["policy_errors"] == []
        [check] = body["checks"]
        return check["status"], check["detail"]

    # Genuine attestations, titled like the required checks: another config, or another (trusted) key.
    impostors = [
        _post_in_toto(review, _envelope(_statement(head, uri=LINT_URI), lint), title="unit-tests"),
        _post_in_toto(review, _envelope(_statement(head, uri=LINT_URI), unit), title="unit-tests"),
        _post_in_toto(review, _envelope(_statement(head, uri=UNIT_URI), lint), title=UNIT_URI),
    ]
    assert [row.status_code for row in impostors] == [201, 201, 201], [row.text for row in impostors]
    status, detail = premerge()
    assert status == "NOT_RUN" and "unit-tests (NOT_RUN)" in detail and f"{UNIT_URI} (NOT_RUN)" in detail

    # The pinned key's own signed result counts by its signed configuration, whatever it is titled.
    own = _post_in_toto(review, _envelope(_statement(head, uri=UNIT_URI, result="FAILED"), unit), title="shown")
    assert own.status_code == 201
    status, detail = premerge()
    assert status == "FAIL" and f"{UNIT_URI} (FAIL)" in detail and "unit-tests (NOT_RUN)" in detail


def test_a_premerge_policy_naming_signed_results_without_a_key_is_refused(tmp_path: Path) -> None:
    signer = _Signer("unit-ci")
    review = _review(tmp_path, base_config=_keys_yaml(signer) + _premerge_yaml(checks=[UNIT_URI], keys=None))
    posted = _post_in_toto(review, _envelope(_statement(_latest(review).head_sha, uri=UNIT_URI), signer))
    assert posted.status_code == 201
    body = _get(review, "premerge")
    assert body["checks"] == [] and any("key_thumbprints" in error for error in body["policy_errors"])
    condition = next(row for row in _get(review, "readiness")["conditions"] if row["id"] == "premerge:policy")
    assert condition["state"] == "unknown"


def _jwk_yaml(*jwks: Mapping[str, Any]) -> str:
    return "attestation_keys:\n" + "".join(f"  - {json.dumps(dict(jwk))}\n" for jwk in jwks)


@pytest.mark.parametrize("trust", ["active", "revoked", "revoked_at_before_ingest", "missing"])
def test_a_stored_signed_result_counts_only_while_the_trusted_base_still_trusts_its_key(
    tmp_path: Path, trust: str
) -> None:
    """Readers re-check the key: a key revoked (or dropped) after ingest stops its rows counting."""

    signer, other = _Signer("release-ci"), _Signer("other-ci")
    lifecycle: dict[str, Any] = {
        "active": {},
        "revoked": {"status": "revoked"},
        "revoked_at_before_ingest": {"revoked_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
    }.get(trust, {})
    listed = [other.jwk] if trust == "missing" else [{**signer.jwk, **lifecycle}, other.jwk]
    config = (
        _jwk_yaml(*listed)
        + _equivalents(_signed_row(signer))
        + _premerge_yaml(checks=[CONFIG_URI], keys=[signer.thumbprint])
    )
    review = _review(tmp_path, base_config=config)
    revision = _latest(review)
    # Recorded while the key was still active (as ingest saw it then).
    ingest, authority = _admit_in_toto(_envelope(_statement(revision.head_sha), signer), _keys(signer))
    row, created = record_check_ingest(review.store, revision, ingest, authority)
    assert created

    counted = trust == "active"
    entry = _entry(_get(review, "run-plan")["plan"], "checks.test")
    assert (entry.get("satisfied_by") or {}).get("evidence_id") == (row.id if counted else None)
    verification = [item for item in _get(review, "verification")["checks"] if item.get("evidence_id") == row.id]
    assert len(verification) == (1 if counted else 0)
    [check] = _get(review, "premerge")["checks"]
    assert check["status"] == ("PASS" if counted else "NOT_RUN")


def test_any_revoked_copy_of_a_key_revokes_it() -> None:
    signer = _Signer("release-ci")
    revoked = {**signer.jwk, "kid": "release-ci-revoked", "status": "revoked"}
    for listed in ([signer.jwk, revoked], [revoked, signer.jwk]):
        keys, errors = trusted_attestation_keys(_jwk_yaml(*listed))
        assert list(keys) == [signer.thumbprint] and keys[signer.thumbprint].revoked_at is not None
        assert len(errors) == 1 and "repeats key" in errors[0]
        with pytest.raises(IngestRefused, match="active key"):
            _admit_in_toto(_envelope(_statement("c" * 40), signer), keys)
    # A revocation past the read limit still revokes a key read within it.
    fillers = [_Signer(f"filler-{index}").jwk for index in range(15)]
    keys, errors = trusted_attestation_keys(_jwk_yaml(signer.jwk, *fillers, revoked))
    assert len(keys) == 16 and keys[signer.thumbprint].revoked_at is not None
    assert any("only the first 16" in error for error in errors)


def test_attestation_key_problems_reach_the_caller(tmp_path: Path) -> None:
    signer, revoked_signer = _Signer("release-ci"), _Signer("old-ci")
    config = _jwk_yaml(
        signer.jwk, revoked_signer.jwk, {**revoked_signer.jwk, "status": "revoked"}, {"kid": "bad", "kty": "RSA"}
    )
    review = _review(tmp_path, base_config=config)
    head = _latest(review).head_sha
    refused = _post_in_toto(review, _envelope(_statement(head), revoked_signer))
    assert refused.status_code == 422
    assert "attestation_keys[2] repeats key" in refused.text and "attestation_keys[3]" in refused.text
    posted = _post_in_toto(review, _envelope(_statement(head), signer))
    assert posted.status_code == 201
    errors = posted.json()["errors"]
    assert [error.split(" ")[0] for error in errors] == ["attestation_keys[2]", "attestation_keys[3]:"]


def test_a_provider_slug_is_normalized_once_and_a_blank_one_is_refused() -> None:
    authority = provider_check_authority(" github ", "ci ", " 9 ")
    assert (authority.provider, authority.integration_id, authority.attested_by) == ("github", "ci", "github:ci@app:9")
    for blank in (" ", "\t"):
        with pytest.raises(IngestRefused, match="needs an authenticated provider"):
            provider_check_authority("github", blank, "9")
