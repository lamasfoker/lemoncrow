"""Offline verification of signed review records against published keys.

A record verifies only when every check passes; there is no partial credit and
no "probably signed":

1. the DSSE envelope parses, its ``payloadType`` is in-toto and its payload is
   the canonical JSON encoding of the Statement (so its digest is the one the
   store reproduces);
2. the Statement satisfies the v1 review-record predicate schema;
3. the payload's own ``signing`` section agrees with the envelope: an unsigned
   record carries no signature (verdict ``unsigned``, never ``verified``); a
   record whose payload claims a signature but carries none, or the reverse, is
   ``invalid``;
4. the signature verifies over the DSSE pre-authentication encoding with the
   published key the payload names, using that key's algorithm;
5. the key was valid for signing at the record's ``issued_at`` (``not_before``
   <= issued_at < ``not_after``) and, when the key was revoked, the record was
   issued before ``revoked_at`` -- otherwise the verdict is ``revoked``. A record
   issued before a key's revocation still verifies and reports the revocation.

Exit codes (``lc review verify-record``): verified 0, invalid 1, unsigned 2,
revoked 3.

Known limit, stated rather than hidden: ``issued_at`` is asserted by the
signer. Whoever holds a key before it is revoked can back-date a record, so a
suspected compromise is revoked *as of* the earliest suspected compromise time
(``revoked_at`` may precede the revocation request), which fails every record
claiming to be issued after it.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
from lemoncrow.pro.capabilities.review.record_signing import SIGNATURE_ALGORITHMS, pae
from lemoncrow.pro.capabilities.review.review_record import (
    PAYLOAD_TYPE,
    canonical_json,
    sha256_digest,
    summarize,
    validate_statement,
)

VerificationStatus = Literal["verified", "unsigned", "invalid", "revoked"]
EXIT_CODES: Final[Mapping[str, int]] = {"verified": 0, "invalid": 1, "unsigned": 2, "revoked": 3}
KEY_STATUSES: Final[tuple[str, ...]] = ("active", "retired", "revoked")
_FUTURE_SKEW: Final[timedelta] = timedelta(minutes=5)
_MAX_KEYS: Final[int] = 1024


@dataclass(frozen=True, slots=True)
class PublishedKey:
    """One public key from a published key set, with its lifecycle."""

    key_id: str
    algorithm: str
    public_key: Any
    status: str
    not_before: datetime | None = None
    not_after: datetime | None = None
    revoked_at: datetime | None = None
    revocation_reason: str = ""
    issuer: str = ""


def _b64url_decode(value: object, *, name: str) -> bytes:
    text = str(value or "")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"key {name} is not base64url") from exc


def _optional_instant(raw: Mapping[str, Any], key: str, kid: str) -> datetime | None:
    value = str(raw.get(key) or "").strip()
    if not value:
        return None
    instant = parse_timestamp(value)
    if instant is None:
        raise ValueError(f"key {kid} has an unreadable {key}")
    return instant


def parse_key(raw: Mapping[str, Any]) -> PublishedKey:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    kid = str(raw.get("kid") or "").strip()
    if not kid:
        raise ValueError("a published key has no kid")
    kty, crv = str(raw.get("kty") or ""), str(raw.get("crv") or "")
    public_key: Any
    if kty == "OKP" and crv == "Ed25519":
        algorithm = "Ed25519"
        public_key = Ed25519PublicKey.from_public_bytes(_b64url_decode(raw.get("x"), name=f"{kid} x"))
    elif kty == "EC" and crv == "P-256":
        algorithm = "ES256"
        x = int.from_bytes(_b64url_decode(raw.get("x"), name=f"{kid} x"), "big")
        y = int.from_bytes(_b64url_decode(raw.get("y"), name=f"{kid} y"), "big")
        public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    else:
        raise ValueError(f"key {kid} has an unsupported type {kty}/{crv}")
    status = str(raw.get("status") or "active")
    if status not in KEY_STATUSES:
        raise ValueError(f"key {kid} has an unknown status {status!r}")
    revoked_at = _optional_instant(raw, "revoked_at", kid)
    if status == "revoked" and revoked_at is None:
        # A revoked key without a revocation time revokes everything it signed.
        revoked_at = datetime.min.replace(tzinfo=UTC)
    return PublishedKey(
        key_id=kid,
        algorithm=algorithm,
        public_key=public_key,
        status="revoked" if revoked_at is not None else status,
        not_before=_optional_instant(raw, "not_before", kid),
        not_after=_optional_instant(raw, "not_after", kid),
        revoked_at=revoked_at,
        revocation_reason=str(raw.get("revocation_reason") or ""),
        issuer=str(raw.get("issuer") or ""),
    )


def parse_key_set(document: object) -> dict[str, PublishedKey]:
    """Published keys by kid; a malformed set raises ``ValueError`` (never a partial set)."""

    if not isinstance(document, Mapping) or not isinstance(document.get("keys"), list):
        raise ValueError("a key set is a JSON object with a keys array")
    rows = document["keys"]
    if len(rows) > _MAX_KEYS:
        raise ValueError(f"a key set holds at most {_MAX_KEYS} keys")
    keys: dict[str, PublishedKey] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("every published key is a JSON object")
        key = parse_key(row)
        if key.key_id in keys:
            raise ValueError(f"key {key.key_id} is published twice")
        keys[key.key_id] = key
    return keys


@dataclass(frozen=True, slots=True)
class RecordVerification:
    status: VerificationStatus
    reasons: tuple[str, ...] = ()
    payload_digest: str = ""
    key_id: str = ""
    key_status: str = ""
    key_revoked_at: str = ""
    signing_state: str = ""
    signing_mode: str = ""
    summary: Mapping[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def headline(self) -> str:
        if self.status == "verified":
            suffix = (
                f"; key {self.key_id} was revoked at {self.key_revoked_at}, after this record was issued"
                if self.key_revoked_at
                else ""
            )
            return f"VERIFIED: signed with key {self.key_id}{suffix}"
        if self.status == "unsigned":
            return "UNSIGNED: this record carries no signature; it proves nothing about who produced it"
        if self.status == "revoked":
            return f"REVOKED: {'; '.join(self.reasons)}"
        return f"INVALID: {'; '.join(self.reasons) or 'the record could not be verified'}"

    def to_wire(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "verified": self.verified,
            "headline": self.headline(),
            "reasons": list(self.reasons),
            "payload_digest": self.payload_digest,
            "key_id": self.key_id,
            "key_status": self.key_status,
            "key_revoked_at": self.key_revoked_at,
            "signing_state": self.signing_state,
            "signing_mode": self.signing_mode,
            "exit_code": self.exit_code,
            "summary": dict(self.summary),
        }


def envelope_of(document: object) -> Mapping[str, Any] | None:
    """The DSSE envelope in a bare envelope file or an API export wrapper."""

    if not isinstance(document, Mapping):
        return None
    if "payloadType" in document:
        return document
    for key in ("envelope", "dsseEnvelope"):
        value = document.get(key)
        if isinstance(value, Mapping):
            return value
    record = document.get("record")
    if isinstance(record, Mapping) and isinstance(record.get("envelope"), Mapping):
        envelope: Mapping[str, Any] = record["envelope"]
        return envelope
    return None


def _instant(value: datetime | None) -> str:
    return "" if value is None or value.year == 1 else value.astimezone(UTC).isoformat()


def _signature_valid(key: PublishedKey, signature: bytes, message: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        if key.algorithm == "Ed25519":
            key.public_key.verify(signature, message)
        elif key.algorithm == "ES256":
            key.public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        else:
            return False
    except (InvalidSignature, ValueError):
        return False
    return True


def verify_record(
    document: object,
    *,
    keys: Mapping[str, PublishedKey] | None,
    now: datetime | None = None,
) -> RecordVerification:
    """Verify one record offline. *keys* is the trusted published key set (``None``: none trusted)."""

    envelope = envelope_of(document)
    if envelope is None:
        return RecordVerification("invalid", ("the file is not a DSSE envelope",))
    if envelope.get("payloadType") != PAYLOAD_TYPE:
        return RecordVerification("invalid", (f"payloadType is not {PAYLOAD_TYPE}",))
    try:
        payload = base64.b64decode(str(envelope.get("payload") or ""), validate=True)
        statement = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return RecordVerification("invalid", ("the payload is not base64 JSON",))
    digest = sha256_digest(payload)
    if not isinstance(statement, dict):
        return RecordVerification("invalid", ("the payload is not a JSON object",), payload_digest=digest)
    try:
        canonical = canonical_json(statement) == payload
    except ValueError:
        canonical = False
    if not canonical:
        return RecordVerification(
            "invalid", ("the payload is not canonical JSON; its digest is not reproducible",), payload_digest=digest
        )
    errors = validate_statement(statement)
    summary = summarize(statement)
    if errors:
        return RecordVerification("invalid", tuple(errors), payload_digest=digest, summary=summary)
    signing = statement["predicate"]["signing"]
    state, mode = str(signing.get("state")), str(signing.get("mode"))
    claimed_kid, claimed_alg = str(signing.get("key_id") or ""), str(signing.get("algorithm") or "")
    base: dict[str, Any] = {"payload_digest": digest, "signing_state": state, "signing_mode": mode, "summary": summary}
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list):
        return RecordVerification("invalid", ("signatures is not a list",), **base)
    if not signatures:
        if state == "unsigned":
            return RecordVerification("unsigned", ("the record is explicitly unsigned",), **base)
        return RecordVerification("invalid", ("the payload claims a signature but the envelope carries none",), **base)
    if state == "unsigned":
        return RecordVerification(
            "invalid", ("the payload says unsigned but the envelope carries a signature",), **base
        )
    if len(signatures) != 1 or not isinstance(signatures[0], Mapping):
        return RecordVerification("invalid", ("a review record carries exactly one signature",), **base)
    entry = signatures[0]
    kid = str(entry.get("keyid") or "")
    if kid != claimed_kid:
        return RecordVerification("invalid", (f"signature key {kid!r} is not the key the payload names",), **base)
    key = (keys or {}).get(kid)
    if key is None:
        return RecordVerification(
            "invalid", (f"no trusted published key {kid}; pass the issuer's key set (--keys)",), key_id=kid, **base
        )
    if claimed_alg not in SIGNATURE_ALGORITHMS or key.algorithm != claimed_alg:
        return RecordVerification(
            "invalid", (f"key {kid} is {key.algorithm}, the payload claims {claimed_alg}",), key_id=kid, **base
        )
    issuer = statement["predicate"].get("issuer")
    issuer_id = str(issuer.get("id") or "") if isinstance(issuer, Mapping) else ""
    if key.issuer and issuer_id != key.issuer:
        return RecordVerification(
            "invalid", (f"key {kid} belongs to issuer {key.issuer}, not {issuer_id}",), key_id=kid, **base
        )
    try:
        signature = base64.b64decode(str(entry.get("sig") or ""), validate=True)
    except (ValueError, binascii.Error):
        return RecordVerification("invalid", ("the signature is not base64",), key_id=kid, **base)
    keyed: dict[str, Any] = {
        "key_id": kid,
        "key_status": key.status,
        "key_revoked_at": _instant(key.revoked_at),
        **base,
    }
    if not _signature_valid(key, signature, pae(PAYLOAD_TYPE, payload)):
        return RecordVerification(
            "invalid", ("the signature does not match the payload (altered after signing or wrong key)",), **keyed
        )
    issued_at = parse_timestamp(statement["predicate"]["issued_at"])
    assert issued_at is not None  # validated by the schema
    clock = now or datetime.now(UTC)
    if issued_at > clock + _FUTURE_SKEW:
        return RecordVerification("invalid", ("the record claims to be issued in the future",), **keyed)
    if key.revoked_at is not None and issued_at >= key.revoked_at:
        return RecordVerification(
            "revoked",
            (
                f"key {kid} was revoked at {_instant(key.revoked_at) or 'an unknown time'} and this record was "
                f"issued at {issued_at.isoformat()}" + (f" ({key.revocation_reason})" if key.revocation_reason else ""),
            ),
            **keyed,
        )
    if key.not_before is not None and issued_at < key.not_before:
        return RecordVerification("invalid", (f"key {kid} was not yet valid when this record was issued",), **keyed)
    if key.not_after is not None and issued_at >= key.not_after:
        return RecordVerification(
            "invalid", (f"key {kid} had stopped signing (retired) before this record was issued",), **keyed
        )
    return RecordVerification("verified", (), **keyed)


__all__ = [
    "EXIT_CODES",
    "KEY_STATUSES",
    "PublishedKey",
    "RecordVerification",
    "VerificationStatus",
    "envelope_of",
    "parse_key",
    "parse_key_set",
    "verify_record",
]
