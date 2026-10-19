"""External evidence adapter SPI (Trust T4, trust plan section 31).

An adapter turns one external payload into LemonCrow's existing typed evidence:
a :class:`~check_ingest.CheckIngest` bound by ``record_check_ingest`` to one
exact Snapshot. It never builds a second evidence model and never sets
readiness: readiness reads the stored row like any other.

The SPI is five questions about the payload (:class:`EvidenceAdapter`):

* ``identify_subject``: the commit the result is about;
* ``verify_authority``: what the adapter can *prove* vouches for it
  (an :class:`AuthorityClaim`);
* ``normalize_result``: the result itself;
* ``normalize_scope``: what the result covered (config, recipe, digests);
* ``normalize_freshness``: when it was observed.

Authority is decided by the server, not the adapter (:func:`admit`):

* :data:`GRANTS` is a fixed, server-side table naming the channels each adapter
  may be used on and the attestation classes it may produce there. A claim
  outside its grant is refused.
* A typed identity comes from what the server authenticated (the channel's
  provider and integration) or from a signature the trusted base's keys verify
  (``signed_attestation``: the integration is the verifying key's RFC 7638
  thumbprint, never a label). A payload never names its own authority.
* Display names are presentation only. Check identity is the typed tuple
  ``(class, provider, integration, stable key)``, so two adapters that produce
  equal titles or tool names can never stand in for each other.

Adapters:

* :class:`ReviewApiAdapter`: SARIF, check annotations or a plain result posted
  through the Review API. Caller-attested: ``human_observation`` or untyped.
* :class:`InTotoAdapter`: a DSSE envelope carrying an in-toto Statement with the
  test-result predicate, verified against keys the trusted base names
  (``attestation_keys`` in ``.lemoncrow/review.yaml``).
* :class:`ProviderCheckAdapter`: a check run or commit status the provider
  reported, on the provider path only. Those rows keep their own dedupe keys, so
  the provider path takes only the authority half, :func:`authorize`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast

import yaml

from lemoncrow.pro.capabilities.review.check_ingest import (
    CHECK_CATEGORIES,
    CheckCategory,
    CheckIngest,
    IngestAuthority,
    IngestRefused,
    parse_check_annotations,
    sarif_ingest,
)
from lemoncrow.pro.capabilities.review.freshness import observation_time
from lemoncrow.pro.capabilities.review.session_models import (
    REVIEW_EVIDENCE_SOURCES,
    ReviewEvidence,
    ReviewEvidenceSource,
)

ChannelKind = Literal["review_api", "provider_webhook"]

#: in-toto over DSSE (the only envelope payload type accepted).
IN_TOTO_PAYLOAD_TYPE: Final[str] = "application/vnd.in-toto+json"
STATEMENT_TYPES: Final[frozenset[str]] = frozenset(
    {"https://in-toto.io/Statement/v1", "https://in-toto.io/Statement/v0.1"}
)
#: The one predicate this adapter reads a result from.
TEST_RESULT_PREDICATE: Final[str] = "https://in-toto.io/attestation/test-result/v0.1"
#: The provider name of a signed in-toto attestation's typed identity.
IN_TOTO_PROVIDER: Final[str] = "in-toto"
SIGNED_ATTESTATION: Final[str] = "signed_attestation"
MAX_ATTESTATION_KEYS: Final[int] = 16
MAX_SIGNATURES: Final[int] = 8
MAX_SUBJECTS: Final[int] = 64
#: The largest signed payload read (decoded bytes); a larger envelope is refused before any signature check.
MAX_SIGNED_PAYLOAD_BYTES: Final[int] = 2 * 1024 * 1024
_MAX_ANNOTATION_ROWS: Final[int] = 5000
_RESULTS: Final[Mapping[str, str]] = {"PASSED": "PASS", "FAILED": "FAIL", "WARNED": "UNKNOWN"}

# --------------------------------------------------------------------------- #
# SPI
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EvidenceChannel:
    """How a payload reached the server, as the server authenticated it. Never read from the payload.

    ``provider`` and ``integration_id`` are set only by an authenticated provider
    path (a verified webhook names the provider app). ``trusted_keys`` are the
    attestation keys the trusted base names, by thumbprint.
    """

    kind: ChannelKind
    source: ReviewEvidenceSource
    actor: str = ""
    provider: str = ""
    integration_id: str = ""
    trusted_keys: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuthorityClaim:
    """What an adapter can prove vouches for its payload; :func:`admit` decides what it is worth."""

    attestation_class: str
    provider: str = ""
    integration_id: str = ""
    attested_by: str = ""
    executor_class: str = ""


@dataclass(frozen=True, slots=True)
class ResultScope:
    """What the result covered: the equivalence key's scope slots."""

    config_digest: str = ""
    recipe: str = ""
    recipe_digest: str = ""


class AuthorityProver(Protocol):
    """The authority half of an adapter (:func:`authorize`)."""

    adapter_id: str

    def verify_authority(self, raw: Mapping[str, Any], channel: EvidenceChannel) -> AuthorityClaim:
        """What vouches for the payload; raises :class:`IngestRefused` when nothing provable does."""
        ...


class EvidenceAdapter(AuthorityProver, Protocol):
    """One external evidence producer (trust plan section 31, SPI)."""

    def identify_subject(self, raw: Mapping[str, Any]) -> str:
        """The commit (head SHA) the result is about."""
        ...

    def normalize_result(self, raw: Mapping[str, Any], *, snapshot_paths: Sequence[str]) -> CheckIngest:
        """The result as one :class:`CheckIngest` (identity fields are overwritten by :func:`admit`)."""
        ...

    def normalize_scope(self, raw: Mapping[str, Any]) -> ResultScope:
        """The scope slots of the result's equivalence key."""
        ...

    def normalize_freshness(self, raw: Mapping[str, Any]) -> str:
        """When the result was observed (ISO-8601), or ``""`` when unknown."""
        ...


@dataclass(frozen=True, slots=True)
class Grant:
    """Server-side registration: where an adapter may be used and what it may produce there."""

    channels: frozenset[str]
    classes: frozenset[str]


#: The only adapters the server admits, with what each may produce. Not configurable:
#: a new adapter or class is a code change with its own impersonation test.
GRANTS: Final[Mapping[str, Grant]] = {
    "review_api": Grant(frozenset({"review_api"}), frozenset({"", "human_observation"})),
    "in_toto": Grant(frozenset({"review_api"}), frozenset({"signed_attestation"})),
    "provider_checks": Grant(frozenset({"provider_webhook"}), frozenset({"provider_ci"})),
}


def _authority(adapter_id: str, claim: AuthorityClaim, channel: EvidenceChannel) -> IngestAuthority:
    """The authority the server grants *claim*, or :class:`IngestRefused`."""

    grant = GRANTS.get(adapter_id)
    if grant is None:
        raise IngestRefused(f"evidence adapter {adapter_id!r} is not registered")
    if channel.kind not in grant.channels:
        raise IngestRefused(f"evidence adapter {adapter_id!r} is not accepted on the {channel.kind} channel")
    kind = claim.attestation_class
    if kind not in grant.classes:
        raise IngestRefused(f"evidence adapter {adapter_id!r} may not produce {kind or 'untyped'} evidence")
    if kind == "provider_ci":
        # The authenticated provider path names the provider and integration, never the payload.
        if not channel.provider or not channel.integration_id:
            raise IngestRefused("provider CI authority needs an authenticated provider and integration")
        if (claim.provider, claim.integration_id) != (channel.provider, channel.integration_id):
            raise IngestRefused("a provider CI claim must name the authenticated provider and integration")
    elif kind == "signed_attestation":
        if claim.provider != IN_TOTO_PROVIDER or claim.integration_id not in channel.trusted_keys:
            raise IngestRefused("a signed attestation must verify with a key the trusted base names")
        # The key vouches for the result, not whoever relayed it.
        return IngestAuthority(
            attestation_class=kind,
            source="external",
            provider=claim.provider,
            integration_id=claim.integration_id,
            attested_by=claim.attested_by,
        )
    elif kind == "human_observation":
        if channel.source != "human":
            raise IngestRefused("only a human may record a human observation")
        return IngestAuthority(attestation_class=kind, source="human", attested_by=channel.actor)
    else:
        return IngestAuthority(attestation_class="", source=channel.source, executor_class=claim.executor_class)
    return IngestAuthority(
        attestation_class=kind,
        source=channel.source,
        provider=claim.provider,
        integration_id=claim.integration_id,
        attested_by=claim.attested_by,
        executor_class=claim.executor_class,
    )


def authorize(adapter: AuthorityProver, raw: Mapping[str, Any], channel: EvidenceChannel) -> IngestAuthority:
    """The authority the server grants what *adapter* proves about *raw* (raises :class:`IngestRefused`)."""

    return _authority(adapter.adapter_id, adapter.verify_authority(raw, channel), channel)


def admit(
    adapter: EvidenceAdapter,
    raw: Mapping[str, Any],
    channel: EvidenceChannel,
    *,
    snapshot_paths: Sequence[str] = (),
) -> tuple[CheckIngest, IngestAuthority]:
    """The normalized result and the authority the server grants it (raises :class:`IngestRefused`).

    Authority is checked before anything else is read: a payload whose
    authority cannot be proven is refused whole, never stored as weaker evidence.
    """

    authority = authorize(adapter, raw, channel)
    ingest = adapter.normalize_result(raw, snapshot_paths=snapshot_paths)
    scope = adapter.normalize_scope(raw)
    return (
        replace(
            ingest,
            head_sha=adapter.identify_subject(raw),
            config_digest=scope.config_digest,
            recipe=scope.recipe,
            recipe_digest=scope.recipe_digest,
            observed_at=adapter.normalize_freshness(raw),
        ),
        authority,
    )


# --------------------------------------------------------------------------- #
# Provider checks (the authenticated provider path)
# --------------------------------------------------------------------------- #


def provider_attested_by(provider: str, integration: str, app_id: str = "") -> str:
    """``provider:integration``, plus ``@app:<id>`` when the provider names a durable app id.

    The integration is the app's display slug, which policy matches. The durable
    app id is recorded beside it, so a slug that later belongs to another app
    can be seen (:func:`attested_app_id`).
    """

    base = f"{provider}:{integration}"
    return f"{base}@app:{app_id}" if app_id else base


def attested_app_id(attested_by: str) -> str:
    """The durable app id :func:`provider_attested_by` recorded, or ``""``."""

    _base, marker, app_id = attested_by.rpartition("@app:")
    return app_id if marker else ""


class ProviderCheckAdapter:
    """A check run or commit status the provider reported for one commit.

    Accepted only on the provider-webhook channel, which the server opens after
    authenticating the delivery (or on its own provider API read). The channel
    names the provider and the integration the provider reported.
    ``raw["integration"]`` must match it, and ``raw["app_id"]`` (the provider's
    durable app id, when it has one) is recorded in ``attested_by``.
    """

    adapter_id = "provider_checks"

    def verify_authority(self, raw: Mapping[str, Any], channel: EvidenceChannel) -> AuthorityClaim:
        integration = str(raw.get("integration") or "").strip()
        return AuthorityClaim(
            attestation_class="provider_ci",
            provider=channel.provider,
            integration_id=integration,
            attested_by=provider_attested_by(channel.provider, integration, str(raw.get("app_id") or "").strip()),
        )


def provider_check_authority(provider: str, integration: str, app_id: str = "") -> IngestAuthority:
    """The ``provider_ci`` authority for a check the provider reported (the provider path only).

    The provider, slug and app id are normalized once here, so the channel and
    the claim always agree; a blank slug is refused (:class:`IngestRefused`).
    """

    provider, integration, app_id = provider.strip(), integration.strip(), app_id.strip()
    channel = EvidenceChannel(kind="provider_webhook", source="external", provider=provider, integration_id=integration)
    return authorize(ProviderCheckAdapter(), {"integration": integration, "app_id": app_id}, channel)


# --------------------------------------------------------------------------- #
# Review API (caller-attested)
# --------------------------------------------------------------------------- #


def _text(raw: Mapping[str, Any], name: str, limit: int) -> str:
    return str(raw.get(name) or "").strip()[:limit]


def _category(raw: Mapping[str, Any], default: str) -> CheckCategory:
    category = str(raw.get("category") or default).strip()
    if category not in CHECK_CATEGORIES:
        raise IngestRefused(f"category must be one of {', '.join(CHECK_CATEGORIES)}")
    return cast(CheckCategory, category)


class ReviewApiAdapter:
    """A result posted through the Review API: SARIF 2.1.0, check annotations or a plain result.

    Caller-attested: the poster is a human (``human_observation``) or nothing
    typed. Every authority field in the body is ignored.
    """

    adapter_id = "review_api"

    def identify_subject(self, raw: Mapping[str, Any]) -> str:
        return str(raw.get("head_sha") or "").strip().lower()

    def verify_authority(self, raw: Mapping[str, Any], channel: EvidenceChannel) -> AuthorityClaim:
        if channel.source == "human":
            return AuthorityClaim(attestation_class="human_observation", attested_by=channel.actor)
        return AuthorityClaim(attestation_class="", executor_class="author_machine")

    def normalize_result(self, raw: Mapping[str, Any], *, snapshot_paths: Sequence[str]) -> CheckIngest:
        category = _category(raw, "ci")
        fmt = str(raw.get("format") or "check").strip()
        title = str(raw.get("title") or "").strip()
        stable_key = str(raw.get("stable_key") or "").strip()
        status = str(raw.get("status") or "").strip().upper()
        common: dict[str, Any] = {
            "head_sha": self.identify_subject(raw),
            "remote_identity": _text(raw, "remote_identity", 160),
            "url": _text(raw, "url", 2000),
        }
        if fmt == "sarif":
            document = raw.get("sarif")
            if not isinstance(document, Mapping):
                raise IngestRefused("sarif must be a SARIF 2.1.0 JSON object")
            return sarif_ingest(
                json.dumps(document).encode("utf-8"),
                category=category,
                title=title,
                stable_key=stable_key,
                status=status,
                snapshot_paths=snapshot_paths,
                **common,
            )
        if fmt in {"check", "github_annotations"}:
            tool = str(raw.get("tool") or title or "check").strip()[:128]
            rows = raw.get("annotations") or []
            if not isinstance(rows, list):
                raise IngestRefused("annotations must be a list")
            findings, truncation = parse_check_annotations(
                [row for row in rows[:_MAX_ANNOTATION_ROWS] if isinstance(row, Mapping)],
                tool=tool,
                snapshot_paths=snapshot_paths,
                reported_total=len(rows),
            )
            return CheckIngest(
                tool=tool,
                category=category,
                status=status or "UNKNOWN",
                status_basis="reported conclusion" if status else "no conclusion reported",
                title=title or tool,
                stable_key=stable_key or title or tool,
                format="github_annotations" if rows else "check",
                findings=findings,
                truncation=truncation,
                **common,
            )
        raise IngestRefused("format must be sarif, check, github_annotations or in_toto")

    def normalize_scope(self, raw: Mapping[str, Any]) -> ResultScope:
        return ResultScope(
            config_digest=_text(raw, "config_digest", 128),
            recipe=_text(raw, "recipe", 160),
            recipe_digest=_text(raw, "recipe_digest", 128),
        )

    def normalize_freshness(self, raw: Mapping[str, Any]) -> str:
        return ""


# --------------------------------------------------------------------------- #
# in-toto / DSSE
# --------------------------------------------------------------------------- #


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def key_thumbprint(key: Any) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url) of a published key's public key.

    Computed from the key material itself, so two encodings of one key share
    it and a label can never stand in for it.
    """

    from cryptography.hazmat.primitives import serialization

    public = key.public_key
    members: dict[str, str]
    if key.algorithm == "Ed25519":
        raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        members = {"crv": "Ed25519", "kty": "OKP", "x": _b64url(raw)}
    elif key.algorithm == "ES256":
        numbers = public.public_numbers()
        members = {
            "crv": "P-256",
            "kty": "EC",
            "x": _b64url(numbers.x.to_bytes(32, "big")),
            "y": _b64url(numbers.y.to_bytes(32, "big")),
        }
    else:  # parse_key admits only the two algorithms above
        raise ValueError(f"unsupported key algorithm {key.algorithm}")
    canonical = json.dumps(members, sort_keys=True, separators=(",", ":")).encode("ascii")
    return _b64url(hashlib.sha256(canonical).digest())


def trusted_attestation_keys(content: str) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Keys by thumbprint from trusted-base ``attestation_keys`` (JWKs), and the problems found.

    Read only from the trusted base: a key added on the head side never
    verifies its own Review. A malformed key is reported and never trusted.
    Only the first :data:`MAX_ATTESTATION_KEYS` rows are trusted, but a
    revoked copy of a trusted key anywhere in the list revokes it (the
    earliest revocation time wins): listing a key twice never un-revokes it.
    """

    from lemoncrow.pro.capabilities.review.record_verify import parse_key

    try:
        decoded = yaml.safe_load(content) if str(content or "").strip() else None
    except yaml.YAMLError:
        return {}, ("attestation_keys: trusted-base review config is not valid YAML",)
    if not isinstance(decoded, Mapping) or decoded.get("attestation_keys") is None:
        return {}, ()
    rows = decoded.get("attestation_keys")
    if not isinstance(rows, list):
        return {}, ("attestation_keys must be a list of JWKs",)
    keys: dict[str, Any] = {}
    errors: list[str] = []
    for index, row in enumerate(rows):
        within = index < MAX_ATTESTATION_KEYS
        if not isinstance(row, Mapping):
            if within:
                errors.append(f"attestation_keys[{index}] must be a JWK mapping")
            continue
        try:
            key = parse_key(row)
        except ValueError as exc:
            if within:
                errors.append(f"attestation_keys[{index}]: {exc}")
            continue
        thumbprint = key_thumbprint(key)
        existing = keys.get(thumbprint)
        if existing is not None:
            errors.append(f"attestation_keys[{index}] repeats key {existing.key_id}")
            if key.revoked_at is not None and (existing.revoked_at is None or key.revoked_at < existing.revoked_at):
                keys[thumbprint] = replace(existing, status="revoked", revoked_at=key.revoked_at)
            continue
        if within:
            keys[thumbprint] = key
    if len(rows) > MAX_ATTESTATION_KEYS:
        errors.append(f"attestation_keys: only the first {MAX_ATTESTATION_KEYS} keys are read")
    return keys, tuple(errors)


def trusted_keys_of(config: Any) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Attestation keys (and their problems) of a present trusted-base review config; none while provisional."""

    if config is None or str(getattr(config, "state", "present")) == "provisional":
        return {}, ()
    return trusted_attestation_keys(str(getattr(config, "content", "") or ""))


def _usable(key: Any, now: datetime) -> bool:
    """A key verifies only while active: not revoked, not before its start, not after its end."""

    if key.revoked_at is not None and now >= key.revoked_at:
        return False
    if key.not_before is not None and now < key.not_before:
        return False
    return key.not_after is None or now < key.not_after


def signed_row_trusted(row: ReviewEvidence, keys: Mapping[str, Any]) -> bool:
    """Whether a stored row still counts under the trusted base's current keys.

    Rows of any other class are untouched. A ``signed_attestation`` row counts
    only while the trusted base still lists its verifying key (by thumbprint)
    and that key was valid when the result was signed (its signed time, else
    when it was ingested), as ``record_verify`` judges a record: a revoked key
    with no revocation time revokes everything it signed.
    """

    if row.attestation_class != SIGNED_ATTESTATION:
        return True
    key = keys.get(row.integration_id)
    signed_at = observation_time(row)
    return key is not None and signed_at is not None and _usable(key, signed_at)


def withhold_untrusted_signed(
    evidence: Iterable[ReviewEvidence], review_config: Callable[[], Any]
) -> tuple[ReviewEvidence, ...]:
    """*evidence* without the signed rows whose key the trusted base no longer trusts.

    Every reader that counts a signed result applies this when it reads, so a
    key revoked (or dropped) after ingest stops its rows counting. The trusted
    base config is read only when a signed row is present.
    """

    rows = tuple(evidence)
    if not any(row.attestation_class == SIGNED_ATTESTATION for row in rows):
        return rows
    keys, _errors = trusted_keys_of(review_config())
    return tuple(row for row in rows if signed_row_trusted(row, keys))


@dataclass(frozen=True, slots=True)
class _Verified:
    statement: Mapping[str, Any]
    payload: bytes
    payload_digest: str
    thumbprint: str
    key_label: str
    signature: bytes


class InTotoAdapter:
    """A DSSE-signed in-toto test-result attestation, verified against trusted-base keys.

    Body: ``{"format": "in_toto", "envelope": <DSSE envelope>}`` plus optional
    presentation fields (``title``, ``category``). Everything that decides
    identity or status is read from the signed Statement:

    * subject: exactly one ``gitCommit`` digest, the commit the tests ran on;
    * status: ``predicate.result`` (``PASSED`` -> PASS, ``FAILED`` -> FAIL,
      ``WARNED`` -> UNKNOWN: warnings are neither a pass nor a failure);
    * check identity: the first ``predicate.configuration`` descriptor's ``uri``
      (else ``name``), with the verifying key's thumbprint as the integration;
    * scope: that descriptor's ``sha256`` digest as the config digest.

    The envelope's ``keyid`` is an unauthenticated hint and is never used:
    every trusted key is tried, and the one that verifies names the authority.
    Only the signed content is kept and deduplicated: the payload bytes, their
    type and the one signature that verified (``record_check_ingest`` keys a
    signed observation by payload digest and key thumbprint), so a re-post that
    alters only unsigned envelope bytes is the same observation.
    """

    adapter_id = "in_toto"

    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = now
        # The payload verified last, held so its identity cannot be reused by another object.
        self._verified: tuple[Mapping[str, Any], _Verified] | None = None

    def _verify(self, raw: Mapping[str, Any], keys: Mapping[str, Any]) -> _Verified:
        self._verified = None
        from lemoncrow.pro.capabilities.review.record_signing import pae
        from lemoncrow.pro.capabilities.review.record_verify import _signature_valid

        envelope = raw.get("envelope")
        if not isinstance(envelope, Mapping):
            raise IngestRefused("in_toto needs a DSSE envelope object")
        payload_type = envelope.get("payloadType")
        if payload_type != IN_TOTO_PAYLOAD_TYPE:
            raise IngestRefused(f"the envelope's payloadType must be {IN_TOTO_PAYLOAD_TYPE}")
        encoded = str(envelope.get("payload") or "")
        if len(encoded) > (MAX_SIGNED_PAYLOAD_BYTES + 2) // 3 * 4:
            raise IngestRefused(f"the signed payload is limited to at most {MAX_SIGNED_PAYLOAD_BYTES} bytes")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise IngestRefused("the envelope payload is not base64") from None
        if len(payload) > MAX_SIGNED_PAYLOAD_BYTES:
            raise IngestRefused(f"the signed payload is limited to at most {MAX_SIGNED_PAYLOAD_BYTES} bytes")
        signatures = envelope.get("signatures")
        if not isinstance(signatures, list) or not signatures:
            raise IngestRefused("the envelope carries no signature")
        if len(signatures) > MAX_SIGNATURES:
            raise IngestRefused(f"an envelope carries at most {MAX_SIGNATURES} signatures")
        if not keys:
            raise IngestRefused("the trusted base names no attestation_keys; nothing can verify this attestation")
        now = self._now or datetime.now(UTC)
        message = pae(IN_TOTO_PAYLOAD_TYPE, payload)
        verifier = ""
        signature = b""
        for entry in signatures:
            if not isinstance(entry, Mapping):
                continue
            try:
                signature = base64.b64decode(str(entry.get("sig") or ""), validate=True)
            except (ValueError, binascii.Error):
                continue
            verifier = next(
                (
                    thumbprint
                    for thumbprint, key in keys.items()
                    if _usable(key, now) and _signature_valid(key, signature, message)
                ),
                "",
            )
            if verifier:
                break
        if not verifier:
            raise IngestRefused(
                "no signature verifies with an active key the trusted base names (altered, unknown, revoked or "
                "expired key)"
            )
        try:
            statement = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise IngestRefused("the signed payload is not JSON") from None
        if not isinstance(statement, Mapping) or statement.get("_type") not in STATEMENT_TYPES:
            raise IngestRefused("the signed payload is not an in-toto Statement")
        if statement.get("predicateType") != TEST_RESULT_PREDICATE:
            raise IngestRefused(f"only the {TEST_RESULT_PREDICATE} predicate is read")
        verified = _Verified(
            statement=statement,
            payload=payload,
            payload_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
            thumbprint=verifier,
            key_label=str(keys[verifier].key_id),
            signature=signature,
        )
        self._verified = (raw, verified)
        return verified

    def _statement(self, raw: Mapping[str, Any]) -> _Verified:
        if self._verified is None or self._verified[0] is not raw:  # admit() verifies authority first
            raise IngestRefused("the attestation was not verified")
        return self._verified[1]

    @staticmethod
    def _predicate(verified: _Verified) -> Mapping[str, Any]:
        predicate = verified.statement.get("predicate")
        if not isinstance(predicate, Mapping):
            raise IngestRefused("the test-result predicate is missing")
        return predicate

    @classmethod
    def _configuration(cls, verified: _Verified) -> Mapping[str, Any]:
        configuration = cls._predicate(verified).get("configuration")
        first = configuration[0] if isinstance(configuration, list) and configuration else None
        if not isinstance(first, Mapping):
            raise IngestRefused("the test-result predicate names no configuration; it is the check's identity")
        return first

    def identify_subject(self, raw: Mapping[str, Any]) -> str:
        subjects = self._statement(raw).statement.get("subject")
        if not isinstance(subjects, list) or not subjects or len(subjects) > MAX_SUBJECTS:
            raise IngestRefused(f"the Statement needs 1 to {MAX_SUBJECTS} subjects")
        commits = {
            str(subject["digest"].get("gitCommit") or "").strip().lower()
            for subject in subjects
            if isinstance(subject, Mapping)
            and isinstance(subject.get("digest"), Mapping)
            and subject["digest"].get("gitCommit")
        }
        if len(commits) != 1:
            raise IngestRefused("the Statement must name exactly one gitCommit subject")
        return commits.pop()

    def verify_authority(self, raw: Mapping[str, Any], channel: EvidenceChannel) -> AuthorityClaim:
        verified = self._verify(raw, channel.trusted_keys)
        return AuthorityClaim(
            attestation_class="signed_attestation",
            provider=IN_TOTO_PROVIDER,
            integration_id=verified.thumbprint,
            attested_by=f"{IN_TOTO_PROVIDER}:{verified.key_label}",
        )

    def normalize_result(self, raw: Mapping[str, Any], *, snapshot_paths: Sequence[str]) -> CheckIngest:
        verified = self._statement(raw)
        predicate = self._predicate(verified)
        configuration = self._configuration(verified)
        stable_key = str(configuration.get("uri") or configuration.get("name") or "").strip()[:160]
        if not stable_key:
            raise IngestRefused("the test-result configuration has no uri or name")
        result = str(predicate.get("result") or "").strip().upper()
        if result not in _RESULTS:
            raise IngestRefused("the test-result predicate's result must be PASSED, WARNED or FAILED")
        counts = ", ".join(
            f"{len(value) if isinstance(value, list) else 0} {label}"
            for label, value in (
                ("passed", predicate.get("passedTests")),
                ("warned", predicate.get("warnedTests")),
                ("failed", predicate.get("failedTests")),
            )
        )
        url = str(predicate.get("url") or "")
        return CheckIngest(
            tool="in-toto test-result",
            category=_category(raw, "tests"),
            status=_RESULTS[result],
            status_basis=f"signed in-toto test-result {result} ({counts})",
            head_sha="",
            title=str(raw.get("title") or "").strip()[:160] or stable_key,
            stable_key=stable_key,
            remote_identity=verified.payload_digest,
            url=url if url.startswith("https://") else "",
            format="in_toto",
            # Only the signed content is kept (re-verifiable offline): never the envelope's unsigned extras.
            attestation=json.dumps(
                {
                    "payload_type": IN_TOTO_PAYLOAD_TYPE,
                    "payload_digest": verified.payload_digest,
                    "key_thumbprint": verified.thumbprint,
                    "key_label": verified.key_label,
                    "envelope": {
                        "payloadType": IN_TOTO_PAYLOAD_TYPE,
                        "payload": base64.b64encode(verified.payload).decode("ascii"),
                        "signatures": [{"sig": base64.b64encode(verified.signature).decode("ascii")}],
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def normalize_scope(self, raw: Mapping[str, Any]) -> ResultScope:
        digest = self._configuration(self._statement(raw)).get("digest")
        sha = str(digest.get("sha256") or "").strip().lower() if isinstance(digest, Mapping) else ""
        return ResultScope(config_digest=f"sha256:{sha}"[:128] if sha else "")

    def normalize_freshness(self, raw: Mapping[str, Any]) -> str:
        """The signed finish time, when the accepted predicate defines one.

        The test-result predicate (v0.1) defines none, so a signed result is
        ordered by ingest time. A replayed older result cannot become newer:
        its signed content dedupes to the row it already has.
        """

        return ""


def review_api_channel(source: str, actor: str, *, trusted_keys: Mapping[str, Any] | None = None) -> EvidenceChannel:
    """The channel of a Review API post (the caller is authenticated, its claims are not)."""

    if source not in REVIEW_EVIDENCE_SOURCES:
        raise IngestRefused(f"unknown evidence source {source!r}")
    return EvidenceChannel(
        kind="review_api",
        source=cast(ReviewEvidenceSource, source),
        actor=actor,
        trusted_keys=dict(trusted_keys or {}),
    )


__all__ = [
    "GRANTS",
    "IN_TOTO_PAYLOAD_TYPE",
    "IN_TOTO_PROVIDER",
    "MAX_SIGNED_PAYLOAD_BYTES",
    "SIGNED_ATTESTATION",
    "TEST_RESULT_PREDICATE",
    "AuthorityClaim",
    "EvidenceAdapter",
    "EvidenceChannel",
    "Grant",
    "InTotoAdapter",
    "ResultScope",
    "ReviewApiAdapter",
    "admit",
    "key_thumbprint",
    "review_api_channel",
    "signed_row_trusted",
    "trusted_attestation_keys",
    "trusted_keys_of",
    "withhold_untrusted_signed",
]
