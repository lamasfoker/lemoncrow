"""Requested vs observed effective model identity (Phase 11 item 15a).

A Review Intelligence run records two identities:

* **requested** -- the driver/model LemonCrow asked for. Empty for a
  subscription CLI (claude/codex/opencode) on its own default: LemonCrow never
  guesses which model that default resolves to.
* **observed** -- the model(s) the driver/provider itself reported executing the
  call (Claude ``modelUsage``/``init`` events, Codex session events, an
  OpenAI-compatible or LiteLLM response's ``model``). Recorded only when
  reported, never inferred.

``source`` names which one an assurance property may rely on:

* ``observed``           -- the provider reported the executed model;
* ``pinned``             -- not reported, but LemonCrow pinned an explicit model;
* ``unreported_default`` -- neither: a CLI default, labeled as such;
* ``unverified``         -- a model named by a worker LemonCrow did not observe.

Only ``observed`` and ``pinned`` identities can satisfy an
``independent_ai_reviewer: require`` policy (``independence.py``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

IdentitySource = Literal["observed", "pinned", "unreported_default", "unverified"]
PROVABLE_SOURCES: Final[frozenset[str]] = frozenset({"observed", "pinned"})
UNREPORTED_DEFAULT_LABEL: Final[str] = "provider default (unreported)"

_MAX_MODEL_CHARS: Final[int] = 128
#: Distinct reported models kept per run. A run reporting more is marked
#: ``observed_truncated`` and can never prove independence (nothing is dropped
#: silently).
# lc-debt: at most 4 observed models per run; upgrade path: persist the full set in a
# dedicated column if a driver routinely fans one call across more models.
MAX_OBSERVED_MODELS: Final[int] = 4
_MODEL_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-\[\]]{0,127}$")


def clean_model_name(value: Any) -> str:
    """A bounded, plain model identifier, or ``""`` when *value* is not one."""

    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > _MAX_MODEL_CHARS or not _MODEL_RE.match(text):
        return ""
    return text


# --------------------------------------------------------------------------- #
# model family (conservative)
# --------------------------------------------------------------------------- #

#: Token patterns per vendor family. A model whose tokens match no family, or
#: more than one (a distilled cross-family model), has no known family.
_FAMILY_TOKENS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("anthropic", re.compile(r"claude\d*|sonnet|opus|haiku")),
    ("openai", re.compile(r"gpt\d*[a-z]?|chatgpt|o[134]|codex|davinci")),
    ("google", re.compile(r"gemini|gemma\d*|palm\d*")),
    ("meta", re.compile(r"llama\d*|codellama")),
    ("mistral", re.compile(r"mistral|mixtral|codestral|ministral|magistral|devstral|pixtral")),
    ("alibaba", re.compile(r"qwen\d*|qwq")),
    ("deepseek", re.compile(r"deepseek")),
    ("xai", re.compile(r"grok\d*")),
    ("moonshot", re.compile(r"kimi|moonshot")),
    ("zhipu", re.compile(r"glm\d*|chatglm\d*")),
    ("microsoft", re.compile(r"phi\d*")),
)


def model_family(model: str) -> str:
    """The vendor family of *model*, or ``""`` when unknown or ambiguous."""

    tokens = [token for token in re.split(r"[^a-z0-9]+", model.lower()) if token]
    families = {name for name, pattern in _FAMILY_TOKENS for token in tokens if pattern.fullmatch(token)}
    return next(iter(families)) if len(families) == 1 else ""


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    driver: str
    requested_model: str = ""
    observed_models: tuple[str, ...] = ()
    observed_truncated: bool = False
    #: A model named by an execution authority LemonCrow did not observe
    #: (a customer worker's self-reported trace).
    unverified_model: str = ""

    @property
    def source(self) -> IdentitySource:
        if self.observed_models:
            return "observed"
        if self.requested_model:
            return "pinned"
        if self.unverified_model:
            return "unverified"
        return "unreported_default"

    @property
    def provable(self) -> bool:
        """Whether this identity may back an assurance claim (observed or pinned)."""

        return self.source in PROVABLE_SOURCES and not self.observed_truncated

    @property
    def effective_models(self) -> tuple[str, ...]:
        if self.observed_models:
            return self.observed_models
        if self.requested_model:
            return (self.requested_model,)
        return ()

    @property
    def label(self) -> str:
        if self.source == "observed":
            return ", ".join(self.observed_models) + (" (+more)" if self.observed_truncated else "")
        if self.source == "pinned":
            return self.requested_model
        if self.source == "unverified":
            return f"{self.unverified_model} (worker-reported)"
        return UNREPORTED_DEFAULT_LABEL

    def to_wire(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "requested_model": self.requested_model,
            "observed_models": list(self.observed_models),
            "observed_truncated": self.observed_truncated,
            **({"unverified_model": self.unverified_model} if self.unverified_model else {}),
            "source": self.source,
            "label": self.label,
        }

    @classmethod
    def from_wire(cls, raw: object, *, driver: str = "", requested_model: str = "") -> ModelIdentity:
        """Read a persisted identity; a missing one is the run's requested identity."""

        if not isinstance(raw, Mapping):
            return cls(driver=driver, requested_model=requested_model)
        observed_raw = raw.get("observed_models")
        observed = observed_from(observed_raw if isinstance(observed_raw, list) else ())
        return cls(
            driver=str(raw.get("driver") or driver),
            requested_model=clean_model_name(raw.get("requested_model")) or requested_model,
            observed_models=observed[0],
            observed_truncated=bool(raw.get("observed_truncated")) or observed[1],
        )


def observed_from(values: Iterable[Any]) -> tuple[tuple[str, ...], bool]:
    """Sorted distinct clean model names, bounded; ``True`` when some were past the bound."""

    distinct = sorted({name for value in values if (name := clean_model_name(value))})
    return tuple(distinct[:MAX_OBSERVED_MODELS]), len(distinct) > MAX_OBSERVED_MODELS


# --------------------------------------------------------------------------- #
# reported-model parsing (driver output)
# --------------------------------------------------------------------------- #


def reported_models_from_event(driver: str, event: Mapping[str, Any]) -> tuple[str, ...]:
    """Models one driver output event explicitly reports executing. Never inferred."""

    found: list[str] = []
    event_type = str(event.get("type") or "")
    if driver == "claude":
        if event_type == "system" and str(event.get("subtype") or "") == "init":
            found.append(str(event.get("model") or ""))
        elif event_type == "assistant":
            message = event.get("message")
            if isinstance(message, Mapping):
                found.append(str(message.get("model") or ""))
        elif event_type == "result":
            usage = event.get("modelUsage")
            if isinstance(usage, Mapping):
                found.extend(str(key) for key in usage)
    elif driver == "codex":
        message = event.get("msg")
        if isinstance(message, Mapping) and str(message.get("type") or "") == "session_configured":
            found.append(str(message.get("model") or ""))
        elif event_type in {"session_configured", "thread.started"}:
            found.append(str(event.get("model") or ""))
    return tuple(name for value in found if (name := clean_model_name(value)))


def reported_models_from_output(driver: str, stdout: str) -> tuple[str, ...]:
    """Models reported anywhere in a driver's complete stdout (one JSON document or JSON lines)."""

    found: list[str] = []
    try:
        whole = json.loads(stdout)
    except (TypeError, ValueError):
        whole = None
    documents: list[Any] = [whole] if whole is not None else []
    if whole is None:
        for line in stdout.splitlines():
            text = line.strip()
            if not text.startswith("{"):
                continue
            try:
                documents.append(json.loads(text))
            except ValueError:
                continue
    for document in documents:
        items = document if isinstance(document, list) else [document]
        for item in items:
            if isinstance(item, Mapping):
                found.extend(reported_models_from_event(driver, item))
    return tuple(dict.fromkeys(found))


__all__ = [
    "MAX_OBSERVED_MODELS",
    "PROVABLE_SOURCES",
    "UNREPORTED_DEFAULT_LABEL",
    "IdentitySource",
    "ModelIdentity",
    "clean_model_name",
    "model_family",
    "observed_from",
    "reported_models_from_event",
    "reported_models_from_output",
]
