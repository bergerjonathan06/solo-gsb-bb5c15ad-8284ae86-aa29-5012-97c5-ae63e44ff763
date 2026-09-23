"""Unit tests for the deterministic backtracking resolver."""
from __future__ import annotations

import pytest

from app.resolver import ResolutionInputError, resolve_payload


def _candidate(version: str, deps: dict[str, str] | None = None) -> dict:
    return {"version": version, "dependencies": deps or {}}


def _component(name: str, versions: list[str], deps_by_version=None) -> dict:
    if deps_by_version is None:
        deps_by_version = {}
    return {
        "name": name,
        "candidates": [
            {"version": v, "dependencies": deps_by_version.get(v, {})} for v in versions
        ],
    }


# --------------------------------------------------------------------------- #


def test_simple_satisfiable_prefers_highest():
    payload = {
        "components": [
            _component("app", ["1.0.0"], {"1.0.0": {"lib": "^1.0.0"}}),
            _component("lib", ["1.0.0", "1.5.0", "2.0.0"]),
        ],
    }
    result = resolve_payload(payload)
    assert result["status"] == "satisfiable"
    assert result["selection"] == {"app": "1.0.0", "lib": "1.5.0"}


def test_backtracking_finds_consistent_assignment():
    # app@1 needs lib ^1; app@2 needs lib ^2.
    # lib 1.9.0 needs util ~1.0; lib 2.0.0 needs util ~2.0.
    # util 1.0.0 and 2.0.0 exist, but util 2.0.0 needs a missing feature only
    # via another constraint: force backtracking by making tool pin util <2.
    payload = {
        "components": [
            {
                "name": "app",
                "candidates": [
                    _candidate("2.0.0", {"lib": "^2.0.0"}),
                    _candidate("1.0.0", {"lib": "^1.0.0"}),
                ],
            },
            _component(
                "lib",
                ["1.9.0", "2.0.0"],
                {
                    "1.9.0": {"util": "~1.0.0"},
                    "2.0.0": {"util": "~2.0.0"},
                },
            ),
            _component("util", ["1.0.0", "2.0.0"]),
            _component("tool", ["1.0.0"], {"1.0.0": {"util": "<2.0.0"}}),
        ],
        "roots": ["app", "tool"],
    }
    result = resolve_payload(payload)
    assert result["status"] == "satisfiable"
    assert result["selection"]["app"] == "1.0.0"
    assert result["selection"]["lib"] == "1.9.0"
    assert result["selection"]["util"] == "1.0.0"
    assert result["stats"]["backtracks"] >= 1


def test_lowest_preference():
    payload = {
        "components": [
            _component("a", ["1.0.0"], {"1.0.0": {"b": ">=1.0.0"}}),
            _component("b", ["1.0.0", "1.2.0", "3.0.0"]),
        ],
        "prefer": "lowest",
    }
    result = resolve_payload(payload)
    assert result["selection"]["b"] == "1.0.0"


def test_unsatisfiable_returns_minimal_conflict_chain():
    payload = {
        "components": [
            _component("app", ["1.0.0"], {"1.0.0": {"lib": "^2.0.0"}}),
            _component("other", ["1.0.0"], {"1.0.0": {"lib": "~1.2.0"}}),
            _component("lib", ["1.2.0", "1.5.0", "2.0.0"]),
        ],
        "roots": ["app", "other"],
    }
    result = resolve_payload(payload)
    assert result["status"] == "unsatisfiable"
    conflict = result["conflict"]
    assert conflict["code"] == "no_candidate"
    assert conflict["component"] == "lib"
    assert conflict["considered_versions"] == ["2.0.0", "1.5.0", "1.2.0"]

    ranges = {c["range"] for c in conflict["minimal_constraints"]}
    assert ranges == {"^2.0.0", "~1.2.0"}
    # Every minimal constraint carries provenance (the requiring version).
    for constraint in conflict["minimal_constraints"]:
        assert constraint["required_by"] is not None
        assert constraint["required_by"]["component"] in {"app", "other"}
    assert len(conflict["chains"]) == 2
    joined = "\n".join(conflict["chains"])
    assert "app@1.0.0" in joined and "other@1.0.0" in joined


def test_version_conflict_against_assigned_component():
    # lib is forced to 1.0.0 by pin; app@2 needs lib ^2 — app's only option.
    payload = {
        "components": [
            _component("pin", ["1.0.0"], {"1.0.0": {"lib": "1.0.0"}}),
            _component("app", ["2.0.0"], {"2.0.0": {"lib": "^2.0.0"}}),
            _component("lib", ["1.0.0", "2.0.0"]),
        ],
        "roots": ["pin", "app"],
    }
    result = resolve_payload(payload)
    assert result["status"] == "unsatisfiable"
    conflict = result["conflict"]
    assert conflict["component"] == "lib"
    # No candidate version satisfies the accumulated pin+app constraints.
    assert conflict["code"] == "no_candidate"
    assert set(conflict["considered_versions"]) == {"1.0.0", "2.0.0"}
    # The rejected requirement is the pin that clashes with the chosen lib@2.
    assert conflict["rejected_requirement"] is not None
    assert conflict["rejected_requirement"]["range"] == "1.0.0"
    ranges = {c["range"] for c in conflict["minimal_constraints"]}
    assert ranges == {"^2.0.0", "1.0.0"}


def test_missing_component_conflict():
    payload = {
        "components": [
            _component("app", ["1.0.0"], {"1.0.0": {"ghost": "^1.0.0"}}),
        ],
    }
    result = resolve_payload(payload)
    assert result["status"] == "unsatisfiable"
    conflict = result["conflict"]
    assert conflict["code"] == "component_not_found"
    assert conflict["component"] == "ghost"
    assert len(conflict["minimal_constraints"]) == 1


def test_no_candidates_at_all():
    payload = {"components": [{"name": "empty", "candidates": []}]}
    result = resolve_payload(payload)
    conflict = result["conflict"]
    assert conflict["code"] == "no_candidate"
    assert conflict["minimal_constraints"][0]["range"] == "*"


def test_invalid_semver_raises():
    payload = {"components": [_component("a", ["not-a-version"])]}
    with pytest.raises(ResolutionInputError, match="不是合法 semver"):
        resolve_payload(payload)


def test_invalid_range_raises():
    payload = {
        "components": [
            _component("a", ["1.0.0"], {"1.0.0": {"b": "???"}}),
            _component("b", ["1.0.0"]),
        ]
    }
    with pytest.raises(ResolutionInputError, match="范围"):
        resolve_payload(payload)


def test_duplicate_component_raises():
    payload = {"components": [_component("a", ["1.0.0"]), _component("a", ["2.0.0"])]}
    with pytest.raises(ResolutionInputError, match="重复"):
        resolve_payload(payload)


def test_diamond_dependency_resolves():
    # app -> left -> lib ^1, app -> right -> lib >=1.2 <2
    payload = {
        "components": [
            _component(
                "app",
                ["1.0.0"],
                {"1.0.0": {"left": "^1.0.0", "right": "^1.0.0"}},
            ),
            _component("left", ["1.0.0"], {"1.0.0": {"lib": "^1.0.0"}}),
            _component("right", ["1.0.0"], {"1.0.0": {"lib": ">=1.2.0 <2.0.0"}}),
            _component("lib", ["1.0.0", "1.2.0", "1.9.0", "2.0.0"]),
        ],
    }
    result = resolve_payload(payload)
    assert result["status"] == "satisfiable"
    assert result["selection"]["lib"] == "1.9.0"
    targets = {(e["source"], e["target"]) for e in result["edges"]}
    assert ("left", "lib") in targets and ("right", "lib") in targets


def test_resolution_is_deterministic():
    payload = {
        "components": [
            _component("a", ["1.0.0"], {"1.0.0": {"b": ">=1.0.0"}}),
            _component("b", ["1.0.0", "2.0.0", "3.0.0"]),
        ],
    }
    first = resolve_payload(payload)["selection"]
    second = resolve_payload(payload)["selection"]
    assert first == second == {"a": "1.0.0", "b": "3.0.0"}
