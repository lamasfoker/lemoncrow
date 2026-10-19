"""Unit tests for the verify-before-done completeness detectors (A + B).

These exercise the pure detector logic directly (the end-to-end block decision
is covered by test_verify_before_done_hook.py). The detectors live in the
host-agnostic ``verify_gate`` core; the Claude hook is just one adapter over it.
"""

from __future__ import annotations

import pytest

from lemoncrow.pro.capabilities import verify_gate as vbd

assert vbd.detector_a and vbd.detector_b  # detectors resolve from the shared core


# --- Detector A -------------------------------------------------------------
def test_bare_call_sites_excludes_def_and_dotted(tmp_path) -> None:
    f = tmp_path / "m.py"
    f.write_text(
        "def _af_new(cls, p):\n    return p\nx = _af_new(a)\ny = self._af_new(b)\nz = cls._af_new(c)\n",
        encoding="utf-8",
    )
    sites = vbd._bare_call_sites("_af_new", str(tmp_path))
    assert len(sites) == 1
    assert sites[0].endswith(":3")  # only the bare `x = _af_new(a)` call


def test_detector_a_fires_on_unconverted_callsite(tmp_path) -> None:
    (tmp_path / "permutations.py").write_text(
        "class P:\n"
        "    @classmethod\n"
        "    def _af_new(cls, p):\n"
        "        return p\n"
        "    def mul(self):\n"
        "        return _af_new(x)\n",
        encoding="utf-8",
    )
    diffs = [
        (
            "permutations.py",
            "    @staticmethod\n    def _af_new(perm):",
            "    @classmethod\n    def _af_new(cls, perm):",
        )
    ]
    res = vbd.detector_a(diffs, root=str(tmp_path))
    assert res is not None
    sym, sites = res
    assert sym == "_af_new"
    assert len(sites) == 1


def test_detector_a_silent_when_all_converted(tmp_path) -> None:
    (tmp_path / "permutations.py").write_text(
        "class P:\n"
        "    @classmethod\n"
        "    def _af_new(cls, p):\n"
        "        return p\n"
        "    def mul(self):\n"
        "        return self._af_new(x)\n",
        encoding="utf-8",
    )
    diffs = [
        (
            "permutations.py",
            "    @staticmethod\n    def _af_new(perm):",
            "    @classmethod\n    def _af_new(cls, perm):",
        )
    ]
    assert vbd.detector_a(diffs, root=str(tmp_path)) is None


# --- Detector B -------------------------------------------------------------
if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_contract_change_detector_contract() -> None:
    changed = [
        (
            "sympy/combinatorics/permutations.py",
            "    @staticmethod\n    def _af_new(perm):",
            "    @classmethod\n    def _af_new(cls, perm):",
        )
    ]
    body_only = [("m.py", "    def foo(x):\n        return 1", "    def foo(x):\n        return 2")]
    assert vbd._contract_changed_symbols(changed) == {"_af_new"}
    assert vbd._contract_changed_symbols(body_only) == set()


def test_second_scenario_detector_contract() -> None:
    assert (
        vbd._second_scenario_token("The issue also reproduces if you create the mentioned plot using `scatterplot`.")
        == "scatterplot"
    )
    assert vbd._second_scenario_token("A normal bug report about label formatting.") is None
    assert (
        vbd.detector_b(
            "Also reproduces with `scatterplot`.", ["seaborn/_core/scales.py", "tests/_core/test_scales.py"]
        )[0]
        == "scatterplot"
    )
    assert (
        vbd.detector_b("Also reproduces with `scatterplot`.", ["seaborn/_core/scales.py", "seaborn/utils.py"]) is None
    )
    assert vbd.detector_b("normal report", ["seaborn/_core/scales.py"]) is None
