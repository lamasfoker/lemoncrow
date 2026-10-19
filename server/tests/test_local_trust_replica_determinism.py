"""Trust plan §39.8 item 6 locally: one stored Review gives one trust digest in any process.

Review Status with the trust section on, and the run plan, are read in this
process and then by two fresh processes composing the local server over the
same state root, each with its own hash seed and time zone. The trust section
(its digest included), the readiness conditions hash and the run plan digest
must be equal. A digest that folds in set iteration order, the process's clock
zone or anything else a process owns differs between them.
``enterprise/server/tests/test_trust_replica_determinism_postgres.py`` holds
the same line for two hosted replicas on one PostgreSQL authority.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from test_local_review_inbox_views import REPO, REVIEWER, _comment, _Local, _ok, _review, _run

from lemoncrow.pro.capabilities.review.session_models import ReviewMark
from lemoncrow.pro.capabilities.review.store import ReviewStore

#: One fresh process: compose the local server over the state root, read, print.
_REPLICA = r"""
import asyncio, json, sys
from pathlib import Path

from aiohttp import ClientSession
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig


async def main(state_root: str, review_id: str, local_fs: bool) -> None:
    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=Path(state_root), local_no_auth=True, allow_local_fs=local_fs)
    )
    url = await server.start()
    try:
        async with ClientSession() as client:
            answers = []
            for suffix in ("/readiness", "/run-plan"):
                async with client.get(f"{url}/api/reviews/{review_id}{suffix}") as response:
                    assert response.status == 200, await response.text()
                    answers.append(await response.json())
    finally:
        await server.stop()
    print(json.dumps(answers))


asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] == "local-fs"))
"""


def _projections(status: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    trust = status["trust"]
    assert trust["summary"]["source"] == "workflow", trust["summary"]
    return {"trust": trust, "conditions_hash": status["conditions_hash"], "plan": plan["plan"]["digest"]}


def _replica(
    state_root: Path,
    review_id: str,
    *,
    hash_seed: str,
    zone: str,
    extra_env: Mapping[str, str] | None = None,
    local_fs: bool = False,
) -> dict[str, Any]:
    env = {
        **os.environ,
        "PYTHONHASHSEED": hash_seed,
        "TZ": zone,
        "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
        **(extra_env or {}),
    }
    done = subprocess.run(
        [sys.executable, "-c", _REPLICA, str(state_root), review_id, "local-fs" if local_fs else "no-fs"],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    status, plan = json.loads(done.stdout.strip().splitlines()[-1])
    return _projections(status, plan)


def test_a_stored_review_gives_one_trust_and_plan_digest_in_every_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_REVIEW_TRUST", "on")
    seen: dict[str, Any] = {}

    async def body(local: _Local) -> None:
        store: ReviewStore = local.store()
        review_id, revision_id = _review(store, "replicas", units=6, repo_root=f"local:{REPO}")
        for index in (0, 2, 3):
            store.set_mark(
                ReviewMark(
                    review_id=review_id,
                    unit_key=f"file:replicas-{index}.py",
                    state="reviewed",
                    reviewed_revision_id=revision_id,
                    content_fingerprint=f"fp-replicas-{index}",
                    reviewer_id=REVIEWER,
                )
            )
        _comment(store, review_id, revision_id)
        status = await _ok(await local.get(f"/api/reviews/{review_id}/readiness"))
        plan = await _ok(await local.get(f"/api/reviews/{review_id}/run-plan"))
        seen["review_id"] = review_id
        seen["here"] = _projections(status, plan)

    _run(tmp_path, body)
    here = seen["here"]
    assert len(here["trust"]["items"]) > 1
    state_root = tmp_path / "state"
    for hash_seed, zone in (("1", "Pacific/Kiritimati"), ("4242", "America/Los_Angeles")):
        assert _replica(state_root, seen["review_id"], hash_seed=hash_seed, zone=zone) == here, hash_seed
