"""The local path of P15-OBS: observability connectors run by the local server itself.

Settings live in the user-level operator file ``review-observability.json``
(never in a repository); no file configures nothing. Credentials are named
environment variables of the local server's process. Each maintenance pass
opens observations for new production deployments (P15-DEPLOY), runs the due
queries within each window's budget and records the aggregates; the
deployment watch's closer then closes ended windows.

Interpretation (recorded): one local settings file serves every repository
of this local server, so its ``services`` scope every repository's queries.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import utc_now
from lemoncrow.pro.capabilities.review.outcomes.observability import ObservabilitySettings, parse_observability_settings
from lemoncrow.pro.capabilities.review.outcomes.observability_connectors import (
    HttpGet,
    run_observation_plan,
    urllib_get,
)
from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import (
    due_plans,
    observation_projection,
    record_aggregates,
    start_observations,
)

_LOG = logging.getLogger(__name__)

LOCAL_OBSERVABILITY_FILE: Final[str] = "review-observability.json"
LOCAL_SOURCE: Final[str] = "local_operator_policy"


def load_local_observability_settings(root: Path | None = None) -> ObservabilitySettings:
    """The operator's local observability settings; missing -> not configured, invalid -> not configured + error."""

    if root is None:
        from lemoncrow.pro.capabilities.review.fix_agent import settings_root

        root = settings_root()
    path = Path(root) / LOCAL_OBSERVABILITY_FILE
    if not path.is_file():
        return ObservabilitySettings(source=LOCAL_SOURCE, locus="local")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ObservabilitySettings(
            source=LOCAL_SOURCE, locus="local", error=f"{LOCAL_OBSERVABILITY_FILE} could not be read"
        )
    if not isinstance(document, Mapping) or document.get("version") != 1:
        return ObservabilitySettings(
            source=LOCAL_SOURCE, locus="local", error=f"{LOCAL_OBSERVABILITY_FILE} must declare version 1"
        )
    unknown = sorted(set(document) - {"version", "observability"})
    if unknown:
        return ObservabilitySettings(
            source=LOCAL_SOURCE, locus="local", error=f"{LOCAL_OBSERVABILITY_FILE} has unknown field {unknown[0]!r}"
        )
    try:
        return parse_observability_settings(document.get("observability"), source=LOCAL_SOURCE, locus="local")
    except ValueError as exc:
        return ObservabilitySettings(source=LOCAL_SOURCE, locus="local", error=str(exc))


def watch_local_observations(
    store: Any,
    *,
    settings: ObservabilitySettings | None = None,
    http_get: HttpGet = urllib_get,
    environ: Mapping[str, str] | None = None,
    now: str | None = None,
) -> tuple[str, ...]:
    """One bounded pass: open new windows, run due queries, record aggregates. Returns touched Review ids.

    Not configured -> no connector request and nothing written (open windows
    still close through the deployment watch's closer).
    """

    effective = settings if settings is not None else load_local_observability_settings()
    if not effective.configured:
        return ()
    instant = now or utc_now()
    touched: list[str] = []
    try:
        touched.extend(start_observations(store, effective, now=instant))
    except Exception:
        _LOG.warning("production observation start failed", exc_info=True)
    env = os.environ if environ is None else environ
    for plan in due_plans(store, now=instant):
        review_id = str(plan.get("review_id") or "")
        try:
            report = run_observation_plan(plan, effective, environ=env, now=instant, http_get=http_get)
            if not report.aggregates:
                continue
            result = record_aggregates(store, review_id, str(plan.get("scope") or ""), report.aggregates, now=instant)
            if result.accepted:
                touched.append(review_id)
            touched.extend(link.review_id for link in result.incidents)
        except Exception:
            # Never the exception text: a transport error may carry the endpoint.
            _LOG.warning("production observation pass failed for Change %s", review_id)
    return tuple(dict.fromkeys(touched))


def local_observation_projection(store: Any, session: Any, *, root: Path | None = None) -> dict[str, Any]:
    return observation_projection(store, session, load_local_observability_settings(root))


__all__ = [
    "LOCAL_OBSERVABILITY_FILE",
    "load_local_observability_settings",
    "local_observation_projection",
    "watch_local_observations",
]
