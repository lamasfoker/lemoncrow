from __future__ import annotations

from benchmarks.review_live_runtime import Sample, summarize


def test_review_live_runtime_summary_reports_distribution_without_latency_thresholds() -> None:
    samples = [
        Sample(10.0, 20.0, 1.0, 5.0, 0.5),
        Sample(20.0, 30.0, 2.0, 6.0, 0.7),
        Sample(30.0, 40.0, 3.0, 7.0, 0.9),
    ]

    result = summarize(samples)

    assert result["samples"] == 3
    assert result["metrics"]["first_diff_ready_ms"] == {
        "median_ms": 20.0,
        "p95_ms": 29.0,
        "min_ms": 10.0,
        "max_ms": 30.0,
    }
    assert result["metrics"]["feedback_handoff_accept_ms"]["median_ms"] == 0.7
