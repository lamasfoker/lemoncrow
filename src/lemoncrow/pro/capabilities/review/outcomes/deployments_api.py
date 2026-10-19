"""``GET /reviews/{id}/deployments`` -- the Review Status deployment line (P15-DEPLOY).

Registered by ``register_review_api`` through one call. The route reuses the
Review API's session gate (scope, existence) and grants no authority; it only
reads. ``{"configured": false}`` (plus the connector's honest status) is the
whole answer when no deployment connector is configured.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter


def register_deployment_routes(
    router: APIRouter,
    *,
    session: Callable[[str], Any],
    projection: Callable[[Any], Mapping[str, Any]],
) -> None:
    @router.get("/reviews/{review_id}/deployments")
    def review_deployments(review_id: str) -> dict[str, Any]:
        return dict(projection(session(review_id)))


__all__ = ["register_deployment_routes"]
