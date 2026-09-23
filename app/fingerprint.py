"""Deterministic request fingerprinting for idempotent reuse."""
from __future__ import annotations

import hashlib
import json

from semantic_version import Version

# Bump when the resolution semantics change so old cached results never leak.
SCHEMA_VERSION = "resolver-v1"


def _candidate_sort_key(candidate: dict) -> tuple:
    """Semver ordering with a stable fallback for (later-rejected) bad input."""
    raw = candidate["version"]
    try:
        return (0, tuple(Version.coerce(raw)))
    except (ValueError, TypeError):
        return (1, raw)


def canonical_payload(payload: dict) -> dict:
    """Return a semantically canonical copy of the submitted request.

    The dependency graph is unordered, so component order and dependency
    declaration order are normalised away. Candidate version order only
    matters for ``prefer=declared``; for highest/lowest the resolver sorts
    versions itself, so the submitted order is normalised (sorted by semver).
    Ranges are normalised to the canonical form the resolver uses
    (empty/None => "*").
    """
    prefer = payload.get("prefer", "highest")

    components: dict[str, dict] = {}
    for comp in payload["components"]:
        name = comp["name"]
        raw_candidates = []
        for cand in comp.get("candidates", []):
            deps = cand.get("dependencies") or {}
            raw_candidates.append(
                {
                    "version": cand["version"],
                    "deps": {
                        dep: (rng or "*") for dep, rng in sorted(deps.items())
                    },
                }
            )

        if prefer != "declared":
            raw_candidates.sort(key=_candidate_sort_key)

        candidates = [
            {"version": c["version"], "dependencies": c["deps"]}
            for c in raw_candidates
        ]
        components[name] = {"candidates": candidates}

    canonical = {"schema": SCHEMA_VERSION, "components": components, "prefer": prefer}

    roots = payload.get("roots")
    if roots is not None:
        # Root order is semantically irrelevant; keep the set deterministically.
        canonical["roots"] = sorted(roots)
    return canonical


def request_fingerprint(payload: dict) -> str:
    """SHA-256 fingerprint of the canonical request JSON."""
    canonical = canonical_payload(payload)
    blob = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
