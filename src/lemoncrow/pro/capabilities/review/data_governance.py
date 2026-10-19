"""Operator-attested data governance: model route trust, provider terms, storage region and egress.

LemonCrow cannot observe where a model provider keeps prompts, whether it
trains on them, which region holds Review data or whether the deployment can
reach the internet. Those are operator attestations, read from one strict JSON
document named by ``LEMONCROW_DATA_GOVERNANCE_FILE``:

.. code-block:: json

    {
      "version": 1,
      "storage": {"region": "eu-west-1"},
      "network": {"egress": "proxy_only"},
      "model_routes": {
        "litellm": {"trust": "customer_local", "provider": "acme-vllm", "data_region": "eu-west-1",
                    "training": "no_training", "retention_days": 0},
        "claude": {"training": "no_training", "retention_days": 30}
      }
    }

Rules:

* Anything not attested is ``unknown``. Nothing is inferred from a hostname,
  an endpoint URL or a provider's name: a hostname is never proof of trust.
* The document is all-or-nothing. A document that cannot be read or parsed,
  or that has an unknown key or value, attests nothing, and every route whose
  trust it could change becomes ``unknown`` (fail closed). Its error text and
  its path are never echoed: either may quote the file.
* Agent CLI routes (claude, codex, opencode) are always ``external``; only
  their provider terms may be attested.

:class:`RouteGovernance` is the reusable route projection: enforcement
(:func:`process_route_trust`, the input of ``content_access_for_path``) and
the data-flow answer read the same value, so they cannot disagree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import Any, Final, Literal, cast

from lemoncrow.pro.capabilities.review.model_identity import clean_model_name
from lemoncrow.pro.capabilities.review.rules import ModelRouteTrust, model_route_trust

DATA_GOVERNANCE_ENV: Final[str] = "LEMONCROW_DATA_GOVERNANCE_FILE"
DATA_GOVERNANCE_VERSION: Final[int] = 1
MAX_DOCUMENT_BYTES: Final[int] = 64 * 1024
MAX_RETENTION_DAYS: Final[int] = 36500

#: Drivers whose trust the operator may attest (their endpoint is operator-configured).
TRUST_ATTESTABLE_DRIVERS: Final[frozenset[str]] = frozenset({"litellm", "openai", "ollama"})
#: Drivers that are always external: only their provider terms may be attested.
TERMS_ONLY_DRIVERS: Final[frozenset[str]] = frozenset({"claude", "codex", "opencode"})
ATTESTABLE_DRIVERS: Final[frozenset[str]] = TRUST_ATTESTABLE_DRIVERS | TERMS_ONLY_DRIVERS

GovernanceState = Literal["not_configured", "attested", "unreadable"]
TrainingUse = Literal["no_training", "may_train"]
EgressAttestation = Literal["no_egress", "proxy_only", "unrestricted"]
TrustSource = Literal["operator_attested", "driver_classifier", "attestation_unreadable"]

TRAINING_USES: Final[tuple[TrainingUse, ...]] = ("no_training", "may_train")
EGRESS_ATTESTATIONS: Final[tuple[EgressAttestation, ...]] = ("no_egress", "proxy_only", "unrestricted")
ATTESTED_TRUSTS: Final[tuple[ModelRouteTrust, ...]] = ("customer_local", "external")

#: A display label: lowercase, no dot, colon, slash or ``@``, so a URL, host or credential cannot pass.
_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_OPAQUE_RUN_CHARS: Final[int] = 12
_OPAQUE_SWITCHES: Final[int] = 3
_ROUTE_KEYS: Final[frozenset[str]] = frozenset({"trust", "provider", "data_region", "training", "retention_days"})
_UNREADABLE: Final[str] = (
    f"{DATA_GOVERNANCE_ENV} could not be read ({{kind}}); error text withheld: it may quote the file"
)


@dataclass(frozen=True, slots=True)
class RouteAttestation:
    """What the operator attests about one driver's route (``None``/``""``: not attested)."""

    trust: ModelRouteTrust | None = None
    provider: str = ""
    data_region: str = ""
    training: TrainingUse | None = None
    retention_days: int | None = None


@dataclass(frozen=True, slots=True)
class DataGovernanceAttestations:
    """The operator's data-governance document as read by this process."""

    state: GovernanceState
    storage_region: str = ""
    egress: EgressAttestation | None = None
    #: ``(driver, attestation)`` pairs, sorted by driver.
    routes: tuple[tuple[str, RouteAttestation], ...] = ()
    #: Why the document attests nothing although it is configured (never its error text or path).
    reason: str = ""

    def route(self, driver: str) -> RouteAttestation | None:
        return next((attested for name, attested in self.routes if name == driver), None)


NOT_CONFIGURED: Final[DataGovernanceAttestations] = DataGovernanceAttestations("not_configured")


@dataclass(frozen=True, slots=True)
class RouteGovernance:
    """One model route's effective trust, model-data locus and provider terms.

    The reusable route projection: any consumer that sends content to a model
    route (Review Intelligence, Ask, background work) reads ``trust`` from here
    and passes it to ``content_access_for_path``; the data-flow answer renders
    the rest. ``trust`` is also the model-data locus (where content goes):
    ``customer_local``, ``external`` or ``unknown``.
    """

    driver: str
    trust: ModelRouteTrust
    trust_source: TrustSource
    provider: str = ""
    data_region: str = ""
    training: TrainingUse | None = None
    retention_days: int | None = None

    @property
    def terms_known(self) -> bool:
        return self.training is not None and self.retention_days is not None

    def to_wire(self) -> dict[str, Any]:
        """Wire form; every fact that is not attested is the string ``unknown``."""

        return {
            "driver": self.driver,
            "trust": self.trust,
            "data_locus": self.trust,
            "trust_source": self.trust_source,
            "provider": self.provider or "unknown",
            "data_region": self.data_region or "unknown",
            "training": self.training or "unknown",
            "retention_days": self.retention_days if self.retention_days is not None else "unknown",
        }


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _object(raw: object, allowed: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("expected an object")
    if set(raw) - allowed:
        raise ValueError("unknown key")
    return raw


def _opaque(run: str) -> bool:
    """A long letter-and-digit run that keeps switching between the two: a token, not a name."""

    switches = sum(1 for left, right in pairwise(run) if left.isdigit() != right.isdigit())
    return len(run) >= _OPAQUE_RUN_CHARS and switches >= _OPAQUE_SWITCHES


#: Leetspeak folded before the secret-word check (``s3cr3t`` -> ``secret``).
_LEET: Final[dict[int, int]] = str.maketrans("013457$@", "oieastsa")
_SECRET_WORDS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "apikey",
    "bearer",
    "credential",
    "privatekey",
)
#: A letter/digit run this long with no separator is a token, not a name.
_LONG_RUN_CHARS: Final[int] = 24
#: Letter/digit switches in one run that no model or label name has (``s3cr3t``, ``a1b2c3``).
_DENSE_SWITCHES: Final[int] = 4


def _credential_shaped(value: str) -> bool:
    from lemoncrow_client.kit.redaction import redact

    from lemoncrow.core.scrubbing import scrub_string

    # lc-debt: credential shapes are the shared scrubbers, a secret-word list (leet folded) and
    # letter/digit run heuristics, so a short plain-word secret can still pass; upgrade path: a closed
    # provider/region/model vocabulary.
    if scrub_string(redact(value)) != value:
        return True
    lowered = value.lower()
    folded = re.sub(r"[^a-z]", "", lowered.translate(_LEET))
    if any(word in folded for word in _SECRET_WORDS):
        return True
    for run in re.findall(r"[a-z0-9]+", lowered):
        switches = sum(1 for left, right in pairwise(run) if left.isdigit() != right.isdigit())
        if _opaque(run) or len(run) >= _LONG_RUN_CHARS or switches >= _DENSE_SWITCHES:
            return True
    return False


def _label(raw: object) -> str:
    """A provider or region label, refused when it is shaped like a URL, host or credential."""

    if not isinstance(raw, str) or not _LABEL_RE.fullmatch(raw):
        raise ValueError("a label must be a short lowercase slug")
    if _credential_shaped(raw):
        raise ValueError("a label must not be shaped like a credential")
    return raw


def plain_name(value: object) -> str:
    """*value* when it is a plain name, else ``""``.

    Letters, digits, ``-`` and ``_`` with at least one letter (an all-digit
    name may be a decimal address), and no credential shape. For
    operator-chosen identifiers a projection may show, such as a customer
    worker or route label.
    """

    if not isinstance(value, str) or not _NAME_RE.fullmatch(value) or not re.search(r"[A-Za-z]", value):
        return ""
    return "" if _credential_shaped(value) else value


#: Provider prefixes a shown model id may carry (``ollama/llama3``); any other ``x/`` is withheld.
# lc-debt: a fixed prefix list and an id shape, so a bare single-label host name (``internal-gw``) still
# reads as a model id and other providers' ids are withheld; upgrade path: a model-id vocabulary per provider.
MODEL_VENDOR_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "anthropic",
        "azure",
        "bedrock",
        "cohere",
        "deepseek",
        "fireworks_ai",
        "gemini",
        "google",
        "groq",
        "meta-llama",
        "mistral",
        "mistralai",
        "ollama",
        "ollama_chat",
        "openai",
        "openrouter",
        "qwen",
        "together_ai",
        "vertex_ai",
        "xai",
    }
)
#: ``[vendor/]model[:tag]``: one optional allowlisted prefix, a name, and a tag that is not a port.
_SHOWN_MODEL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:(?P<vendor>[a-z0-9][a-z0-9_-]{0,31})/)?"
    r"(?P<name>[a-z0-9][a-z0-9._-]{0,95})"
    r"(?::(?P<tag>[a-z0-9][a-z0-9._-]{0,31}))?"
)


def _host_shaped(segment: str) -> bool:
    """A segment that could name a host or address: a dot not between two digits,
    a dotted number run, a hex literal, ``localhost`` or no letter at all."""

    return (
        re.search(r"(?<!\d)\.|\.(?!\d)", segment) is not None
        or re.search(r"\d+(?:\.\d+){2,}", segment) is not None
        or re.search(r"(?:^|[^a-z0-9])0x[0-9a-f]+", segment) is not None
        or "localhost" in segment
        or re.search(r"[a-z]", segment) is None
    )


def shown_model_name(value: object) -> str:
    """*value* when it is a model identifier a projection may show, else ``""``.

    An allowlist shape, never a host blocklist: ``[vendor/]name[:tag]`` with the
    vendor in :data:`MODEL_VENDOR_PREFIXES`, a tag that has a letter (so never a
    port), and no ``@``, brackets, ``+``, second ``/`` or second ``:``. A dot is
    allowed only between two digits (``gpt-3.5``, ``qwen2.5:1.5b``), so a
    hostname cannot pass; a name without a letter, a hex literal, a dotted number run or
    a credential shape is withheld. Legitimate but odd ids (Bedrock's
    ``anthropic.claude-...-v1:0``) are withheld too: never shown is the safe side.
    """

    name = clean_model_name(value)
    match = _SHOWN_MODEL_RE.fullmatch(name.lower()) if name else None
    if match is None or _credential_shaped(name):
        return ""
    vendor, model, tag = match.group("vendor"), match.group("name"), match.group("tag")
    if vendor is not None and vendor not in MODEL_VENDOR_PREFIXES:
        return ""
    if _host_shaped(model) or (tag is not None and _host_shaped(tag)):
        return ""
    return name


def _choice(raw: object, choices: tuple[str, ...]) -> str:
    if not isinstance(raw, str) or raw not in choices:
        raise ValueError("value outside the vocabulary")
    return raw


def _route(driver: str, raw: object) -> RouteAttestation:
    entry = _object(raw, _ROUTE_KEYS)
    if "trust" in entry and driver not in TRUST_ATTESTABLE_DRIVERS:
        raise ValueError("agent CLI routes are always external")
    days = entry.get("retention_days")
    if days is not None and (
        isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_RETENTION_DAYS
    ):
        raise ValueError("retention_days must be a whole number of days")
    return RouteAttestation(
        trust=cast(ModelRouteTrust, _choice(entry["trust"], ATTESTED_TRUSTS)) if "trust" in entry else None,
        provider=_label(entry["provider"]) if "provider" in entry else "",
        data_region=_label(entry["data_region"]) if "data_region" in entry else "",
        training=cast(TrainingUse, _choice(entry["training"], TRAINING_USES)) if "training" in entry else None,
        retention_days=days,
    )


def parse_data_governance(text: str) -> DataGovernanceAttestations:
    """Parse one document strictly; raises ``ValueError`` (never quoting it) on anything unexpected."""

    document = _object(
        json.loads(text, object_pairs_hook=_unique_keys), frozenset({"version", "storage", "network", "model_routes"})
    )
    version = document.get("version")
    if type(version) is not int or version != DATA_GOVERNANCE_VERSION:
        raise ValueError("unsupported version")
    storage = _object(document.get("storage", {}), frozenset({"region"}))
    network = _object(document.get("network", {}), frozenset({"egress"}))
    routes = _object(document.get("model_routes", {}), ATTESTABLE_DRIVERS)
    return DataGovernanceAttestations(
        "attested",
        storage_region=_label(storage["region"]) if "region" in storage else "",
        egress=(
            cast(EgressAttestation, _choice(network["egress"], EGRESS_ATTESTATIONS)) if "egress" in network else None
        ),
        routes=tuple(sorted((str(driver), _route(str(driver), raw)) for driver, raw in routes.items())),
    )


_CACHE_LOCK: Final[threading.Lock] = threading.Lock()
_cache: dict[str, tuple[tuple[int, int, int], DataGovernanceAttestations]] = {}


def _read(path: str) -> DataGovernanceAttestations:
    stat = os.stat(path)
    key = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        cached = _cache.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    with open(path, "rb") as handle:
        raw = handle.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("document too large")
    parsed = parse_data_governance(raw.decode("utf-8"))
    with _CACHE_LOCK:
        # One entry per configured path; a process names one path, so this stays tiny.
        _cache.clear()
        _cache[path] = (key, parsed)
    return parsed


def load_data_governance(environ: Mapping[str, str] | None = None) -> DataGovernanceAttestations:
    """This process's attestations: not configured, attested, or unreadable (fail closed)."""

    env = os.environ if environ is None else environ
    path = str(env.get(DATA_GOVERNANCE_ENV) or "").strip()
    if not path:
        return NOT_CONFIGURED
    try:
        return _read(path)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
        # The message may quote the file or name its path: only the exception class is kept.
        return DataGovernanceAttestations("unreadable", reason=_UNREADABLE.format(kind=type(exc).__name__))


def data_governance_digest(attestations: DataGovernanceAttestations | None = None) -> str:
    """A digest of the effective attestations (default: this process's document).

    The document is read per process, so durable Review jobs bind this digest
    into their policy digest: a worker process whose document differs from
    the one the API server read when it planned the job refuses the job as
    stale authority. It covers what the document attests, never its path,
    formatting or error text; every unreadable document attests the same nothing.
    """

    facts = load_data_governance() if attestations is None else attestations
    effective = {
        "state": facts.state,
        "storage_region": facts.storage_region,
        "egress": facts.egress,
        "routes": [[driver, asdict(route)] for driver, route in facts.routes],
    }
    return hashlib.sha256(json.dumps(effective, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# the route projection
# --------------------------------------------------------------------------- #


def normalize_driver(driver: str) -> str:
    normalized = driver.strip().lower()
    return "openai" if normalized == "openai_compatible" else normalized


def route_governance(driver: str, *, attestations: DataGovernanceAttestations | None = None) -> RouteGovernance:
    """The effective governance of this process's route for *driver*.

    Precedence: an unreadable document makes every trust-attestable route
    ``unknown``; an attested trust comes next; otherwise the driver
    classifier (``rules.model_route_trust``), under which ``ollama``,
    ``litellm`` and any unrecognized driver stay ``unknown``: only an
    attestation makes a route ``customer_local``.
    """

    name = normalize_driver(driver)
    facts = load_data_governance() if attestations is None else attestations
    classified = model_route_trust(name)
    if facts.state == "unreadable":
        if name in TRUST_ATTESTABLE_DRIVERS:
            return RouteGovernance(name, "unknown", "attestation_unreadable")
        return RouteGovernance(name, classified, "driver_classifier")
    attested = facts.route(name)
    if attested is None:
        return RouteGovernance(name, classified, "driver_classifier")
    trust: ModelRouteTrust = attested.trust if attested.trust is not None else classified
    return RouteGovernance(
        name,
        trust,
        "operator_attested" if attested.trust is not None else "driver_classifier",
        provider=attested.provider,
        data_region=attested.data_region,
        training=attested.training,
        retention_days=attested.retention_days,
    )


def process_route_trust(driver: str, *, explicit: ModelRouteTrust | None = None) -> ModelRouteTrust:
    """The trust of this process's route for *driver*: what content-access enforcement reads.

    ``explicit`` (a route trust recorded elsewhere, such as a customer worker
    route policy) wins, exactly like ``rules.model_route_trust``.
    """

    if explicit is not None:
        return explicit
    return route_governance(driver).trust


__all__ = [
    "ATTESTABLE_DRIVERS",
    "DATA_GOVERNANCE_ENV",
    "DATA_GOVERNANCE_VERSION",
    "EGRESS_ATTESTATIONS",
    "MAX_DOCUMENT_BYTES",
    "MODEL_VENDOR_PREFIXES",
    "NOT_CONFIGURED",
    "TERMS_ONLY_DRIVERS",
    "TRAINING_USES",
    "TRUST_ATTESTABLE_DRIVERS",
    "DataGovernanceAttestations",
    "EgressAttestation",
    "GovernanceState",
    "RouteAttestation",
    "RouteGovernance",
    "TrainingUse",
    "TrustSource",
    "data_governance_digest",
    "load_data_governance",
    "normalize_driver",
    "parse_data_governance",
    "plain_name",
    "process_route_trust",
    "route_governance",
    "shown_model_name",
]
