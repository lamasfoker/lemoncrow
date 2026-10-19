"""Mixed-version compatibility through the Review API (Phase 9R item 41, ``compat.mixed_version``).

Persistence pairings execute in the real sandbox (bubblewrap); API and
message-format pairings are evaluated on the review host from the contract
documents. Gating (Deep or a policy floor), ``not_available`` and the budget
use the same planned-execution path as every other analysis.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.gateway.test_review_artifact_analyses_api import (
    _analyses,
    _behavior,
    _ok,
    _plan_entry,
    _record,
    _run,
)
from tests.gateway.test_review_check_runs_api import HEADERS, _compute_runs, _review
from tests.gateway.test_review_hermetic import needs_bwrap

ENTRY = "compat.mixed_version"
DEEP = "review:\n  effort: deep\n"
INIT = (
    "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, legacy TEXT);\n"
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, total INTEGER);\n"
)
OLD_CODE = (
    "def load(db, user_id):\n"
    '    return db.execute("SELECT id, email, legacy FROM users WHERE id = %s", (user_id,)).fetchone()\n'
    "\n"
    "def total(db, user_id):\n"
    "    return db.execute('SELECT SUM(total) FROM orders WHERE user_id = ?', (user_id,)).fetchone()\n"
)
NEW_CODE = (
    "def load(db, user_id):\n"
    '    return db.execute("SELECT id, email FROM users WHERE id = %s", (user_id,)).fetchone()\n'
    "\n"
    "def total(db, user_id):\n"
    "    return db.execute('SELECT SUM(total) FROM orders WHERE user_id = ?', (user_id,)).fetchone()\n"
)
BASE = {
    ".lemoncrow/review.yaml": DEEP,
    "db/migrations/0001_init.up.sql": INIT,
    "db/migrations/0001_init.down.sql": "DROP TABLE orders;\nDROP TABLE users;\n",
    "app/db.py": OLD_CODE,
}


def _spec_record(review: SimpleNamespace, spec: str) -> dict[str, Any]:
    return _record(review, ENTRY, spec)


def _pairings(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["pairing"]: row for row in record["summary"]["pairings"]}


def _compat_targets(review: SimpleNamespace) -> list[dict[str, Any]]:
    return [row for row in _behavior(review) if row["kind"] == "compatibility"]


# --------------------------------------------------------------------------- #
# persistence: old code against the migrated schema (sandboxed)
# --------------------------------------------------------------------------- #


@needs_bwrap
@pytest.mark.parametrize(
    ("migration", "error"),
    [
        ("ALTER TABLE users DROP COLUMN legacy;\n", "no such column: legacy"),
        ("ALTER TABLE users RENAME COLUMN legacy TO legacy_note;\n", "no such column: legacy"),
    ],
    ids=["drop", "rename"],
)
def test_a_migration_that_drops_or_renames_a_column_old_code_reads_is_caught(
    tmp_path: Path, migration: str, error: str
) -> None:
    head = {
        "db/migrations/0002_legacy.up.sql": migration,
        "db/migrations/0002_legacy.down.sql": "SELECT 1;\n",
        "app/db.py": NEW_CODE,
    }
    review = _review(tmp_path, base=BASE, head=head)
    entries = {row["id"]: row for row in _analyses(review)["entries"]}
    assert [spec["id"] for spec in entries[ENTRY]["specs"]] == ["persistence-db-migrations"]

    report = _ok(review, ENTRY)
    assert report["verdict"] == "FAIL" and report["executions"] == 1
    record = _spec_record(review, "persistence-db-migrations")
    pairings = _pairings(record)
    forward = pairings["old_code_new_schema"]
    assert (forward["state"], forward["direction"], forward["incompatible"]) == ("incompatible", "forward", 1)
    # The new code no longer reads it: the reverse pairing holds.
    assert pairings["new_code_old_schema"]["state"] == "compatible"
    assert record["summary"]["altered_tables"] == ["users"]
    finding = next(row for row in record["findings"] if row["rule"] == "old_statement_fails")
    assert finding["critical"] is True and finding["paths"][0] == "app/db.py"
    assert error in json.dumps(finding["facts"])
    # The untouched orders statement is not paired (no altered table), never flagged.
    assert not [row for row in record["findings"] if "orders" in json.dumps(row)]
    # Evidence before judgment: a critical compatibility target citing the record and its sandbox run.
    targets = _compat_targets(review)
    assert len(targets) == 1 and targets[0]["critical"] is True
    detail = review.client.get(
        f"/api/reviews/{review.review_id}/behavior/targets/{targets[0]['behavior_key']}",
        headers=HEADERS,
    )
    assert detail.status_code == 200, detail.text
    refs = [ref["ref"] for ref in detail.json()["target"]["evidence"]["refs"]]
    assert f"artifact:{record['evidence_id']}" in refs
    assert _compute_runs(review) == {f"{ENTRY}@local": 1}
    verdict = review.store.get_evidence(report["verdict_evidence"])
    assert (verdict.stable_key, verdict.verification_status) == (ENTRY, "FAIL")


@needs_bwrap
def test_an_insert_that_omits_a_new_required_column_is_caught(tmp_path: Path) -> None:
    base = {
        **BASE,
        "app/write.py": "def add(db):\n    db.execute('INSERT INTO orders (id, user_id) VALUES (?, ?)', (1, 2))\n",
    }
    head = {
        "db/migrations/0002_currency.up.sql": "ALTER TABLE orders ADD COLUMN currency TEXT NOT NULL;\n",
        "db/migrations/0002_currency.down.sql": "ALTER TABLE orders DROP COLUMN currency;\n",
    }
    review = _review(tmp_path, base=base, head=head)
    assert _ok(review, ENTRY)["verdict"] == "FAIL"
    record = _spec_record(review, "persistence-db-migrations")
    rules = {row["rule"] for row in record["findings"]}
    assert "old_insert_misses_required_column" in rules


@needs_bwrap
def test_a_compatible_change_passes_and_shows_its_deploy_order(tmp_path: Path) -> None:
    head = {
        # Additive and nullable: the old code keeps working on the migrated schema.
        "db/migrations/0002_nickname.up.sql": "ALTER TABLE users ADD COLUMN nickname TEXT;\n",
        "db/migrations/0002_nickname.down.sql": "ALTER TABLE users DROP COLUMN nickname;\n",
        "app/db.py": OLD_CODE
        + '\ndef nick(db, user_id):\n    return db.execute("SELECT nickname FROM users WHERE id = %s", (user_id,))\n',
    }
    review = _review(tmp_path, base=BASE, head=head)
    report = _ok(review, ENTRY)
    record = _spec_record(review, "persistence-db-migrations")
    pairings = _pairings(record)
    assert report["verdict"] == "PASS", record["reasons"]
    assert pairings["old_code_new_schema"]["state"] == "compatible"
    # The new code needs the column: a deploy-order requirement, not a failure.
    reverse = pairings["new_code_old_schema"]
    assert (reverse["state"], reverse["direction"]) == ("incompatible", "reverse")
    assert record["summary"]["deploy_order"] == ["run the migration before deploying the new code"]
    targets = _compat_targets(review)
    assert [row["critical"] for row in targets] == [False]


@needs_bwrap
def test_old_code_without_literal_sql_is_not_available_never_a_pass(tmp_path: Path) -> None:
    base = {
        **BASE,
        "app/db.py": "class User(Base):\n    __tablename__ = 'users'\n    legacy = Column(String)\n",
    }
    head = {
        "db/migrations/0002_legacy.up.sql": "ALTER TABLE users DROP COLUMN legacy;\n",
        "db/migrations/0002_legacy.down.sql": "SELECT 1;\n",
    }
    review = _review(tmp_path, base=base, head=head)
    report = _ok(review, ENTRY)
    record = _spec_record(review, "persistence-db-migrations")
    forward = _pairings(record)["old_code_new_schema"]
    assert forward["state"] == "not_available" and "app/db.py" in forward["reason"]
    assert report["verdict"] in {"NOT_RUN", "UNKNOWN"} and report["verdict"] != "PASS"


def test_unconstructible_pairings_are_not_available_with_their_reason(tmp_path: Path) -> None:
    base = {
        ".lemoncrow/review.yaml": DEEP,
        "app/alembic/versions/0001_init.py": "revision = '0001'\n",
        "api/schema.graphql": "type Query { user: User }\ntype User { id: ID! email: String }\n",
        "app/main.py": "print(1)\n",
    }
    head = {
        "app/alembic/versions/0002_drop.py": "revision = '0002'\n",
        "api/schema.graphql": "type Query { user: User }\ntype User { id: ID! }\n",
    }
    review = _review(tmp_path, base=base, head=head)
    report = _ok(review, ENTRY)
    assert report["verdict"] == "NOT_RUN" and report["executions"] == 0
    persistence = _spec_record(review, "persistence-app-alembic-versions")
    assert {row["state"] for row in persistence["summary"]["pairings"]} == {"not_available"}
    assert "Alembic" in persistence["summary"]["pairings"][0]["reason"]
    graphql = _spec_record(review, "api-graphql")
    assert {row["state"] for row in graphql["summary"]["pairings"]} == {"not_available"}
    assert "GraphQL" in graphql["reasons"][0]
    assert {row["stable_key"]: row["status"] for row in _analyses(review)["verdicts"]} == {ENTRY: "NOT_RUN"}


@pytest.mark.parametrize("open_state", ["unknown", "not_available", "limited_by_budget"])
def test_a_compatible_pairing_beside_an_open_one_is_never_a_pass(open_state: str) -> None:
    from lemoncrow.pro.capabilities.review import compat_mixed_version as compat
    from lemoncrow.pro.capabilities.review.analysis_common import AnalysisSpec

    spec = AnalysisSpec(entry_id=ENTRY, id="api-openapi", tool="openapi")
    pairings = [
        compat.Pairing("old_client_new_api", "compatible", "every old-client call is still served"),
        compat.Pairing("new_client_old_api", open_state, "could not be decided"),  # type: ignore[arg-type]
    ]
    evaluation = compat.evaluate(spec, compat.Prepared(kind="api", pairings=pairings), None)
    assert evaluation.status == "UNKNOWN", evaluation.reasons
    # Every pairing compatible (or not applicable) is the only pass.
    pairings[1] = compat.Pairing("new_client_old_api", "not_applicable", "")
    assert compat.evaluate(spec, compat.Prepared(kind="api", pairings=pairings), None).status == "PASS"


def test_a_repository_without_a_contract_surface_is_inapplicable_even_at_deep(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        base={".lemoncrow/review.yaml": DEEP, "app/main.py": "print(1)\n"},
        head={"app/main.py": "print(2)\n"},
    )
    entry = _plan_entry(review, ENTRY)
    assert entry is not None and entry["status"] == "inapplicable", entry
    assert "no migrations, API contract or message format to pair" in entry["reason"]
    assert _run(review, ENTRY).status_code == 409


def test_an_untouched_contract_surface_is_not_applicable(tmp_path: Path) -> None:
    review = _review(tmp_path, base=BASE, head={"README.md": "docs only\n"})
    report = _ok(review, ENTRY)
    assert report["verdict"] == "PASS" and report["executions"] == 0
    record = _spec_record(review, "persistence-db-migrations")
    assert {row["state"] for row in record["summary"]["pairings"]} == {"not_applicable"}


# --------------------------------------------------------------------------- #
# API: old client against the new API (contract documents and R3 routes)
# --------------------------------------------------------------------------- #

OPENAPI = {
    "openapi": "3.0.0",
    "info": {"title": "svc", "version": "1"},
    "paths": {
        "/users/{id}": {
            "get": {
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}}}
                },
            }
        },
        "/users": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"email": {"type": "string"}},
                                "required": ["email"],
                            }
                        }
                    }
                },
                "responses": {"201": {"description": "created"}},
            }
        },
        "/legacy": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
    "components": {
        "schemas": {
            "User": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "email": {"type": "string"}},
            }
        }
    },
}
ROUTES = (
    "from flask import Flask\napp = Flask(__name__)\n\n"
    '@app.get("/users/<id>")\ndef user(id):\n    return {}\n\n'
    '@app.get("/export")\ndef export():\n    return {}\n'
)


def _api_review(tmp_path: Path, new_doc: dict[str, Any], routes: str = ROUTES) -> SimpleNamespace:
    base = {".lemoncrow/review.yaml": DEEP, "api/openapi.json": json.dumps(OPENAPI), "app/routes.py": ROUTES}
    return _review(tmp_path, base=base, head={"api/openapi.json": json.dumps(new_doc), "app/routes.py": routes})


def test_an_api_change_that_breaks_the_old_client_is_caught(tmp_path: Path) -> None:
    new = json.loads(json.dumps(OPENAPI))
    del new["paths"]["/legacy"]
    new["components"]["schemas"]["User"]["properties"].pop("email")
    new["paths"]["/users/{id}"]["get"]["parameters"].append(
        {"name": "tenant", "in": "query", "required": True, "schema": {"type": "string"}}
    )
    new["paths"]["/teams"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    # The code stops serving /export (R3 route facts, endpoint level).
    review = _api_review(tmp_path, new, routes=ROUTES.split('\n\n@app.get("/export")')[0] + "\n")
    report = _ok(review, ENTRY)
    assert report["verdict"] == "FAIL" and report["executions"] == 0  # evaluated on the review host
    record = _spec_record(review, "api")
    assert record["summary"]["evaluated_on"] == "review_host"
    rules = {(row["rule"], row["subject"].rsplit("::", 1)[-1]) for row in record["findings"]}
    assert ("removed_operation", "GET /legacy") in rules
    assert ("removed_response_field", "GET /users/{id} 200.email") in rules
    assert ("new_required_parameter", "GET /users/{id} query:tenant") in rules
    assert ("removed_route", "GET /export") in rules
    assert ("added_operation", "GET /teams") in rules  # reverse: deploy the API before its clients
    pairings = _pairings(record)
    assert pairings["old_client_new_api"]["state"] == "incompatible"
    assert pairings["new_client_old_api"]["state"] == "incompatible"
    assert record["summary"]["deploy_order"] == ["deploy the API before the clients that call it"]
    critical = {row["title"] for row in _compat_targets(review) if row["critical"]}
    assert "Removes an API operation · GET /legacy" in critical
    assert not any("GET /teams" in title for title in critical)


def test_a_compatible_api_change_passes(tmp_path: Path) -> None:
    new = json.loads(json.dumps(OPENAPI))
    new["components"]["schemas"]["User"]["properties"]["nickname"] = {"type": "string"}
    new["paths"]["/users/{id}"]["get"]["parameters"].append(
        {"name": "verbose", "in": "query", "required": False, "schema": {"type": "boolean"}}
    )
    review = _api_review(tmp_path, new)
    report = _ok(review, ENTRY)
    record = _spec_record(review, "api")
    assert report["verdict"] == "PASS", record["reasons"]
    assert {row["state"] for row in record["summary"]["pairings"]} == {"compatible"}
    assert record["findings"] == [] and _compat_targets(review) == []


def test_an_unparseable_contract_is_not_available(tmp_path: Path) -> None:
    base = {".lemoncrow/review.yaml": DEEP, "api/openapi.yaml": "openapi: 3.0.0\npaths: {}\n"}
    review = _review(tmp_path, base=base, head={"api/openapi.yaml": "openapi: [unclosed\n"})
    report = _ok(review, ENTRY)
    record = _spec_record(review, "api")
    assert report["verdict"] == "NOT_RUN"
    assert {row["state"] for row in record["summary"]["pairings"]} == {"not_available"}


# --------------------------------------------------------------------------- #
# messages: new consumer against old-format messages
# --------------------------------------------------------------------------- #

PROTO = 'syntax = "proto3";\nmessage Order {\n  string id = 1;\n  int64 total = 2;\n}\n'
AVRO = {
    "type": "record",
    "name": "Payment",
    "namespace": "shop",
    "fields": [{"name": "id", "type": "string"}, {"name": "amount", "type": "int"}],
}


def test_message_format_changes_pair_consumers_with_old_messages(tmp_path: Path) -> None:
    base = {".lemoncrow/review.yaml": DEEP, "proto/order.proto": PROTO, "schemas/payment.avsc": json.dumps(AVRO)}
    avro = json.loads(json.dumps(AVRO))
    avro["fields"].append({"name": "currency", "type": "string"})  # no default: old data lacks it
    avro["fields"][1]["type"] = "long"  # a promotion: compatible
    head = {
        "proto/order.proto": PROTO.replace("int64 total = 2", "string total = 2"),
        "schemas/payment.avsc": json.dumps(avro),
    }
    review = _review(tmp_path, base=base, head=head)
    assert _ok(review, ENTRY)["verdict"] == "FAIL"
    record = _spec_record(review, "messages")
    rules = {(row["rule"], row["subject"].rsplit("::", 1)[-1]) for row in record["findings"]}
    assert rules == {
        ("field_type_changed", "Order.total=2"),
        ("new_field_without_default", "shop.Payment.currency"),
    }
    assert _pairings(record)["new_consumer_old_messages"]["state"] == "incompatible"
    assert _pairings(record)["old_consumer_new_messages"]["state"] == "compatible"

    compatible = _review(
        tmp_path / "ok",
        base=base,
        head={
            "schemas/payment.avsc": json.dumps(
                {**AVRO, "fields": [*AVRO["fields"], {"name": "note", "type": "string", "default": ""}]}
            )
        },
    )
    assert _ok(compatible, ENTRY)["verdict"] == "PASS"


# --------------------------------------------------------------------------- #
# gating and budget
# --------------------------------------------------------------------------- #


def test_the_check_runs_only_at_deep_or_under_a_policy_floor(tmp_path: Path) -> None:
    for case_index, (review_config, admitted, required) in enumerate(
        [
            ("review:\n  effort: auto\n", False, False),
            ("review:\n  effort: thorough\n", False, False),
            (DEEP, True, False),
            ("review:\n  effort: auto\n  required:\n    - compat.mixed_version\n", True, True),
            ("review:\n  effort: auto\n  min_effort: deep\n", True, True),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        new = json.loads(json.dumps(OPENAPI))
        del new["paths"]["/legacy"]
        base = {".lemoncrow/review.yaml": review_config, "api/openapi.json": json.dumps(OPENAPI)}
        review = _review(case_tmp_path, base=base, head={"api/openapi.json": json.dumps(new)})
        entry = _plan_entry(review, ENTRY)
        response = _run(review, ENTRY)
        if not admitted:
            assert entry is None or entry["status"] != "run", entry
            assert response.status_code == 409, response.text
            assert not [row for row in _analyses(review)["results"] if row["entry_id"] == ENTRY]
            return
        assert entry is not None and entry["status"] == "run" and (entry["required"] is required), entry
        assert response.status_code == 200, response.text
        assert response.json()["verdict"] == "FAIL"


@needs_bwrap
def test_an_exhausted_budget_limits_the_sandboxed_pairing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import artifact_analysis_api

    monkeypatch.setattr(artifact_analysis_api, "analysis_budget_seconds", lambda _plan, _entry: 0.0)
    head = {
        "db/migrations/0002_legacy.up.sql": "ALTER TABLE users DROP COLUMN legacy;\n",
        "db/migrations/0002_legacy.down.sql": "SELECT 1;\n",
    }
    review = _review(tmp_path, base=BASE, head=head)
    report = _ok(review, ENTRY)
    assert report["verdict"] == "NOT_RUN" and report["executions"] == 0
    record = _spec_record(review, "persistence-db-migrations")
    assert {row["state"] for row in record["summary"]["pairings"]} == {"limited_by_budget"}
    assert record["reasons"][0].startswith("Limited by budget")
