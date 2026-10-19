from __future__ import annotations

import pytest
from pydantic import ValidationError

from lemoncrow.core.foundation.models import Playbook, Rubric, Trace


def _block(**kw: object) -> Playbook:
    base: dict[str, object] = dict(id="b1", title="t", domain="coding", situation="s", procedure=["do thing"])
    base.update(kw)
    return Playbook(**base)


def test_foundation_model_validation_contract() -> None:
    """case: test_procedure_must_be_non_empty"""
    with pytest.raises(ValidationError):
        _block(procedure=[])
    "case: test_extra_fields_forbidden"
    with pytest.raises(ValidationError):
        Playbook(id="b1", title="t", domain="coding", situation="s", procedure=["x"], extra_field="nope")
    "case: test_trace_extra_forbidden"
    with pytest.raises(ValidationError):
        Trace(id="t1", agent="a", domain="coding", task="x", status="success", mystery=1)
    "case: test_rubric_extra_forbidden"
    with pytest.raises(ValidationError):
        Rubric(id="r1", domain="coding", required_checks=["c"], something_else=True)


def test_playbook_identity_and_success_contract() -> None:
    """case: test_make_id_is_stable"""
    a = Playbook.make_id("Hello World", "coding")
    b = Playbook.make_id("Hello World", "coding")
    assert a == b
    assert a.startswith("coding-hello-world")
    "case: test_success_rate"
    b = _block(success_count=3, failure_count=1)
    assert b.success_rate() == 0.75
    b2 = _block()
    assert b2.success_rate() == 0.0
