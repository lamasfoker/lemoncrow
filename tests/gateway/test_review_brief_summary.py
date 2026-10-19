from lemoncrow.pro.capabilities.review.brief_summary import build_brief_summary


def projection(items=(), intents=()):
    return {
        "items": list(items),
        "plan": {"intents": list(intents)},
        "coverage": {"file_count": 134, "target_count": 590},
        "questions": [],
        "actions": [],
        "decisions": [],
    }


def explanation(text, **overrides):
    return {
        "id": "item:change",
        "kind": "behavior",
        "rationale_origin": "author",
        "after": text,
        "stale": False,
        **overrides,
    }


def test_summary_distills_context_without_progress_chatter_or_claimed_test_results():
    value = projection(
        [
            explanation("Each branch keeps its review. Additional implementation detail."),
            explanation("", id="agent:progress", title="I'll inspect the implementation."),
            explanation(
                "", id="agent:done", title="Implemented branch-local review identity", rationale="Tests: 5 passed."
            ),
            explanation("", id="agent:conversation", title="Yes, the Brief supports AI annotations."),
        ]
    )
    result = build_brief_summary(value)
    assert result["paragraphs"] == ["Each branch keeps its review.", "Implemented branch-local review identity"]
    assert result["verification"] == "No current verification results recorded."
    assert result["origin"] == "author"


def test_summary_uses_current_recorded_checks_not_stale_or_agent_claims():
    checks = [
        {"name": "unit", "status": "PASS", "state": "current"},
        {"name": "unit", "status": "PASS", "state": "current"},
        {"name": "browser", "status": "FAIL", "state": "current"},
        {"name": "old", "status": "PASS", "state": "stale"},
        {"name": "lint", "status": "NOT_RUN", "state": "current"},
    ]
    result = build_brief_summary(projection(), checks)
    assert result["verification"] == "Recorded checks: 1 passed, 1 failed, 1 unresolved."


def test_summary_is_bounded_and_reports_explanations_beyond_the_bound():
    for count in (3, 4, 50):
        result = build_brief_summary(projection([explanation(f"Change {i}.") for i in range(count)]))
        assert len(result["paragraphs"]) == min(3, count)
        assert result["additional_explanations"] == max(0, count - 3)
    result = build_brief_summary(projection([explanation("Changed " + "long sentence " * 200)]))
    assert len(result["paragraphs"][0]) <= 280


def test_summary_excludes_stale_explanations_and_labels_inferred_text():
    result = build_brief_summary(
        projection(
            [
                explanation("Obsolete implementation.", stale=True),
                explanation("Now preserves review identity.", rationale_origin="inferred"),
            ]
        )
    )
    assert result["paragraphs"] == ["Now preserves review identity."]
    assert result["origin"] == "inferred"


def test_summary_without_ai_or_agent_explanation_only_describes_observed_scope():
    areas = [
        {"kind": "area", "title": name, "primary_target_ids": [name], "rationale_origin": "none"}
        for name in ("Review", "Frontend", "Server", "Docs")
    ]
    result = build_brief_summary(projection(areas))
    assert result["paragraphs"] == ["Changes span Review, Frontend, Server and 1 other area."]
    assert result["origin"] == "observed"
    assert result["scope"] == "134 files · 590 changed targets"


def test_summary_deduplicates_intent_and_keeps_actionable_counts():
    value = projection(
        [explanation("Keep branch history."), explanation("Fixed review identity.")],
        [{"intent": "Keep branch history."}],
    )
    value["decision_progress"] = {"outstanding": 2}
    value["questions"] = [{"title": "Check migration"}]
    result = build_brief_summary(value)
    assert result["paragraphs"] == ["Keep branch history.", "Fixed review identity."]
    assert result["additional_explanations"] == 0
    assert result["attention"] == "2 open decisions; 1 issue to investigate"


def test_summary_keeps_prioritized_changes_visible_without_a_plan_question():
    result = build_brief_summary(
        projection(
            [
                explanation("Fixed branch identity.", attention="review_first", primary_target_ids=["t-1", "t-2"]),
                explanation("Changed storage.", attention="review_first", primary_target_ids=["t-2"]),
            ]
        )
    )
    assert result["attention"] == "2 changed targets need attention"


def test_empty_intent_does_not_inflate_omitted_explanation_count():
    result = build_brief_summary(
        projection(
            [explanation("Fixed branch identity.")],
            [{"intent": ""}],
        )
    )
    assert result["additional_explanations"] == 0
