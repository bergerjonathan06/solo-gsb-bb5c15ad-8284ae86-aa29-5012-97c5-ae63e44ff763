"""Dependency graph construction and deterministic backtracking resolver.

The resolver implements a depth-first constraint search:

* Components are picked with the Minimum Remaining Values (MRV) heuristic,
  ties broken by component name — the choice is therefore fully deterministic.
* Candidate versions are tried in a fixed order (highest / lowest / declared).
* Every component's accepted constraints are annotated with their provenance
  (requirer version + dependency path from a root).
* On failure the deepest dead-end is kept and reduced to an irreducible
  "minimal killer" subset of constraints, which drives the reported
  minimal conflict chain.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from semantic_version import NpmSpec, Version

# Ranges may be left empty, meaning "any version".
ANY_SPEC = "*"


class ResolutionInputError(ValueError):
    """Raised for malformed submissions (maps to HTTP 422)."""


@dataclass(frozen=True)
class Node:
    component: str
    version: str  # raw version string used in the submitted manifest


@dataclass(frozen=True)
class Requirement:
    """One semver constraint imposed on a component, with provenance."""

    range: str
    required_by: Node | None  # None => declared by a root
    path: tuple[Node, ...]    # chain from a root to `required_by` (exclusive)
    spec: NpmSpec


@dataclass
class Candidate:
    node: Node
    version: Version
    deps: dict[str, Requirement]  # name -> requirement imposed by this version


@dataclass
class Catalog:
    # component -> candidate versions in the order they will be tried
    candidates: dict[str, list[Candidate]]
    roots: list[str]
    prefer: str

    def total_candidates(self) -> int:
        return sum(len(c) for c in self.candidates.values())


# --------------------------------------------------------------------------- #
# Catalog construction / validation
# --------------------------------------------------------------------------- #


def build_catalog(
    components: list[dict],
    roots: list[str] | None,
    prefer: str,
) -> Catalog:
    """Validate the submitted manifest and build the candidate catalog."""
    candidates: dict[str, list[Candidate]] = {}

    seen_names: set[str] = set()
    for comp in components:
        name = comp["name"]
        if name in seen_names:
            raise ResolutionInputError(f"组件 {name!r} 在清单中重复出现")
        seen_names.add(name)

        raw_candidates: list[tuple[str, dict[str, str]]] = []
        seen_versions: set[str] = set()
        for cand in comp["candidates"]:
            raw = cand["version"]
            try:
                parsed = Version.coerce(raw)
            except ValueError as exc:
                raise ResolutionInputError(
                    f"组件 {name!r} 的版本 {raw!r} 不是合法 semver：{exc}"
                ) from exc
            normalized = str(parsed)
            if normalized in seen_versions:
                raise ResolutionInputError(
                    f"组件 {name!r} 的候选版本 {normalized} 重复"
                )
            seen_versions.add(normalized)

            deps: dict[str, str] = {}
            for dep_name, dep_range in (cand.get("dependencies") or {}).items():
                if not dep_name:
                    raise ResolutionInputError(
                        f"组件 {name!r}@{normalized} 存在空名称的依赖"
                    )
                if dep_name == name:
                    raise ResolutionInputError(
                        f"组件 {name!r}@{normalized} 声明了自依赖；"
                        "同一组件的自约束范围无法由单一选中版本同时满足"
                    )
                deps[dep_name] = dep_range or ANY_SPEC
            raw_candidates.append((normalized, deps))

        ordered = _order_candidates(raw_candidates, prefer)
        built: list[Candidate] = []
        for normalized, deps in ordered:
            dep_reqs: dict[str, Requirement] = {}
            for dep_name, dep_range in deps.items():
                try:
                    spec = NpmSpec(dep_range)
                except ValueError as exc:
                    raise ResolutionInputError(
                        f"组件 {name!r}@{normalized} 对 {dep_name!r} 的范围 "
                        f"{dep_range!r} 不合法：{exc}"
                    ) from exc
                dep_reqs[dep_name] = Requirement(
                    range=dep_range,
                    required_by=Node(name, normalized),
                    path=(),  # filled when the candidate is actually selected
                    spec=spec,
                )
            built.append(Candidate(Node(name, normalized), Version.coerce(normalized), dep_reqs))
        candidates[name] = built

    if roots is None:
        root_list = list(candidates)
    else:
        root_list = list(roots)
        unknown_roots = [r for r in root_list if r not in candidates]
        if unknown_roots:
            raise ResolutionInputError(
                f"roots 包含清单中不存在的组件：{', '.join(unknown_roots)}"
            )
        if len(set(root_list)) != len(root_list):
            raise ResolutionInputError("roots 中存在重复组件")
        if not root_list:
            raise ResolutionInputError("roots 不能为空列表（请省略该字段或提供至少一个根）")

    return Catalog(candidates=candidates, roots=root_list, prefer=prefer)


def _order_candidates(
    raw: list[tuple[str, dict[str, str]]], prefer: str
) -> list[tuple[str, dict[str, str]]]:
    if prefer == "declared":
        return list(raw)
    reverse = prefer == "highest"
    return sorted(raw, key=lambda item: Version.coerce(item[0]), reverse=reverse)


# --------------------------------------------------------------------------- #
# Dead-end bookkeeping
# --------------------------------------------------------------------------- #


@dataclass
class DeadEnd:
    depth: int
    code: str
    component: str
    reqs: list[Requirement]
    rejected: Requirement | None
    considered: list[str]
    selected_version: str | None

    def rank(self) -> tuple:
        # deepest first; ties: component name, then the constraint signature
        sig = tuple(sorted(r.range + ":" + _node_key(r.required_by) for r in self.reqs))
        return (-self.depth, self.component, sig)


def _node_key(node: Node | None) -> str:
    return "root" if node is None else f"{node.component}@{node.version}"


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #


class Resolver:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.nodes_visited = 0
        self.backtracks = 0
        self._deadends: list[DeadEnd] = []
        self._spec_cache: dict[tuple[str, str], bool] = {}

    # -- public entrypoint -------------------------------------------------- #

    def resolve(self) -> dict:
        started = time.perf_counter()
        assignment: dict[str, Candidate] = {}
        requirements: dict[str, list[Requirement]] = {
            root: [Requirement(range=ANY_SPEC, required_by=None, path=(), spec=NpmSpec("*"))]
            for root in self.catalog.roots
        }

        ok = self._search(assignment, requirements, depth=0)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)

        stats = {
            "nodes_visited": self.nodes_visited,
            "backtracks": self.backtracks,
            "elapsed_ms": elapsed_ms,
            "component_count": len(self.catalog.candidates),
            "candidate_count": self.catalog.total_candidates(),
        }
        if ok:
            return {
                "status": "satisfiable",
                "selection": {name: cand.node.version for name, cand in assignment.items()},
                "edges": self._build_edges(assignment),
                "conflict": None,
                "stats": stats,
            }

        deadend = min(self._deadends, key=lambda d: d.rank())
        return {
            "status": "unsatisfiable",
            "selection": None,
            "edges": [],
            "conflict": self._build_conflict(deadend, assignment),
            "stats": stats,
        }

    # -- search ------------------------------------------------------------- #

    def _search(
        self,
        assignment: dict[str, Candidate],
        requirements: dict[str, list[Requirement]],
        depth: int,
    ) -> bool:
        # Every component that has accumulated constraints (roots plus every
        # dependency target discovered so far) must be assigned. Components
        # declared in the manifest but unreachable from any root are irrelevant.
        if all(name in assignment for name in requirements):
            return True

        name = self._pick_component(assignment, requirements)
        self.nodes_visited += 1

        # Component referenced but not present in the manifest.
        if name not in self.catalog.candidates:
            self._record_missing(name, requirements.get(name, []), depth)
            return False

        reqs = requirements[name]
        candidates = self.catalog.candidates[name]
        viable = [c for c in candidates if self._satisfies_all(c.version, reqs)]

        # No candidate version meets the accumulated constraints.
        if not viable:
            self._record_no_candidate(name, reqs, candidates, depth)
            return False

        for cand in viable:
            new_reqs = self._propagate(cand, assignment, requirements, depth)
            if new_reqs is None:
                # A propagated requirement directly clashes with an already
                # assigned component — recorded inside _propagate.
                self.backtracks += 1
                continue

            assignment[name] = cand
            if self._search(assignment, new_reqs, depth + 1):
                return True
            del assignment[name]
            self.backtracks += 1

        return False

    def _pick_component(
        self,
        assignment: dict[str, Candidate],
        requirements: dict[str, list[Requirement]],
    ) -> str:
        """MRV: unassigned constrained component with fewest matching versions."""
        best: str | None = None
        best_count: int = -1
        for name, reqs in sorted(requirements.items()):
            if name in assignment:
                continue
            options = self.catalog.candidates.get(name, ())
            count = sum(1 for c in options if self._satisfies_all(c.version, reqs))
            if best is None or count < best_count:
                best, best_count = name, count
        assert best is not None, "no component left to pick"
        return best

    def _propagate(
        self,
        cand: Candidate,
        assignment: dict[str, Candidate],
        requirements: dict[str, list[Requirement]],
        depth: int,
    ) -> dict[str, list[Requirement]] | None:
        """Return the next requirement map, or None if a hard clash occurs."""
        new_reqs = {k: list(v) for k, v in requirements.items()}
        parent_path = self._path_through(cand, requirements.get(cand.node.component, []))

        for dep_name, dep_req in cand.deps.items():
            req = Requirement(
                range=dep_req.range,
                required_by=dep_req.required_by,
                path=parent_path,
                spec=dep_req.spec,
            )
            existing = new_reqs.setdefault(dep_name, [])

            # Dedup identical (source + range) requirements.
            if not any(
                r.required_by == req.required_by and r.range == req.range for r in existing
            ):
                if dep_name in assignment:
                    chosen = assignment[dep_name]
                    if not self._satisfies(chosen.version, req.spec):
                        self._record_clash(
                            dep_name, existing, req, chosen.node.version, depth
                        )
                        return None
                existing.append(req)
        return new_reqs

    def _path_through(self, cand: Candidate, reqs: list[Requirement]) -> tuple[Node, ...]:
        """Deterministically choose the shortest provenance chain to this node."""
        candidates: list[tuple[Node, ...]] = [r.path + (cand.node,) for r in reqs]
        candidates = candidates or [(cand.node,)]
        return min(candidates, key=lambda p: (len(p), tuple(n.component + "@" + n.version for n in p)))

    # -- dead-end recording ------------------------------------------------- #

    def _record_missing(self, name: str, reqs: list[Requirement], depth: int) -> None:
        killers = self._minimal_killers(name, reqs, missing=True)
        self._deadends.append(
            DeadEnd(
                depth=depth + 1,
                code="component_not_found",
                component=name,
                reqs=killers,
                rejected=None,
                considered=[],
                selected_version=None,
            )
        )

    def _record_no_candidate(
        self, name: str, reqs: list[Requirement], candidates: list[Candidate], depth: int
    ) -> None:
        killers = self._minimal_killers(name, reqs)
        self._deadends.append(
            DeadEnd(
                depth=depth + 1,
                code="no_candidate",
                component=name,
                reqs=killers,
                rejected=None,
                considered=[c.node.version for c in candidates],
                selected_version=None,
            )
        )

    def _record_clash(
        self,
        name: str,
        existing: list[Requirement],
        rejected: Requirement,
        selected_version: str,
        depth: int,
    ) -> None:
        killers = self._minimal_killers(name, existing + [rejected])
        hard = not self._feasible(name, existing + [rejected])
        # A "hard" clash means no candidate version at all can satisfy the
        # accumulated constraints: treat it as a deep no_candidate dead-end.
        all_versions = [c.node.version for c in self.catalog.candidates.get(name, ())]
        self._deadends.append(
            DeadEnd(
                depth=depth + 1,
                code="no_candidate" if hard else "version_conflict",
                component=name,
                reqs=killers,
                rejected=rejected,
                considered=all_versions if hard else [selected_version],
                selected_version=selected_version,
            )
        )

    # -- conflict reduction ------------------------------------------------- #

    def _satisfies(self, version: Version, spec: NpmSpec) -> bool:
        key = (str(version), spec.expression)
        cached = self._spec_cache.get(key)
        if cached is None:
            cached = spec.match(version)
            self._spec_cache[key] = cached
        return cached

    def _satisfies_all(self, version: Version, reqs: list[Requirement]) -> bool:
        return all(self._satisfies(version, r.spec) for r in reqs)

    def _feasible(self, name: str, reqs: list[Requirement]) -> bool:
        """True if some known candidate version satisfies all requirements."""
        return any(
            self._satisfies_all(c.version, reqs) for c in self.catalog.candidates.get(name, ())
        )

    def _minimal_killers(
        self, name: str, reqs: list[Requirement], missing: bool = False
    ) -> list[Requirement]:
        """Greedily remove constraints whose absence restores feasibility.

        Result is irreducible relative to the deterministic iteration order
        (root constraints first, then the order constraints were accumulated).
        """
        if missing:
            # A missing component cannot be satisfied by any set of ranges;
            # a single requirement is enough to kill.
            return [self._pick_single(reqs)]
        killers = list(reqs)
        for req in list(reqs):
            trial = [r for r in killers if r is not req]
            if trial and not self._feasible(name, trial):
                killers = trial
        return killers

    @staticmethod
    def _pick_single(reqs: list[Requirement]) -> Requirement:
        return min(
            reqs,
            key=lambda r: (
                1 if r.required_by is None else 0,
                _node_key(r.required_by),
                r.range,
            ),
        )

    # -- output construction ------------------------------------------------ #

    def _build_edges(self, assignment: dict[str, Candidate]) -> list[dict]:
        edges: list[dict] = []
        for name in sorted(assignment):
            cand = assignment[name]
            for dep_name in sorted(cand.deps):
                if dep_name in assignment:
                    dep_cand = assignment[dep_name]
                    edges.append(
                        {
                            "source": name,
                            "source_version": cand.node.version,
                            "target": dep_name,
                            "target_version": dep_cand.node.version,
                            "range": cand.deps[dep_name].range,
                        }
                    )
        return edges

    def _build_conflict(self, deadend: DeadEnd, assignment: dict[str, Candidate]) -> dict:
        constraints = [self._constraint_info(r) for r in deadend.reqs]
        chains = self._build_chains(deadend)

        if deadend.code == "component_not_found":
            message = (
                f"组件 {deadend.component!r} 不存在于提交的清单中，"
                f"但有 {len(constraints)} 条约束要求它"
            )
        elif deadend.code == "no_candidate":
            considered = deadend.considered
            message = (
                f"组件 {deadend.component!r} 的 {len(considered)} 个候选版本中，"
                "没有任何版本同时满足最小冲突约束集"
            )
        else:
            message = (
                f"组件 {deadend.component!r} 已选定版本 {deadend.selected_version}，"
                "无法满足后续约束；尝试其他版本后仍无解"
            )

        return {
            "code": deadend.code,
            "component": deadend.component,
            "message": message,
            "minimal_constraints": constraints,
            "chains": chains,
            "considered_versions": sorted(deadend.considered, key=lambda v: Version.coerce(v), reverse=True),
            "selected_version": deadend.selected_version,
            "rejected_requirement": (
                self._constraint_info(deadend.rejected) if deadend.rejected else None
            ),
        }

    @staticmethod
    def _constraint_info(req: Requirement) -> dict:
        return {
            "range": req.range,
            "required_by": (
                None
                if req.required_by is None
                else {"component": req.required_by.component, "version": req.required_by.version}
            ),
            "path": [
                {"component": n.component, "version": n.version} for n in req.path
            ],
        }

    def _build_chains(self, deadend: DeadEnd) -> list[str]:
        lines: list[str] = []
        for req in deadend.reqs:
            chain = " -> ".join(f"{n.component}@{n.version}" for n in req.path) or "根"
            source = "根声明" if req.required_by is None else f"{req.required_by.component}@{req.required_by.version}"
            lines.append(
                f"{chain} -> 要求 {deadend.component} {req.range}（约束来源: {source}）"
            )
        return lines


# --------------------------------------------------------------------------- #
# Convenience
# --------------------------------------------------------------------------- #


def resolve_payload(payload: dict) -> dict:
    """Validate + resolve an API request payload. Returns the result envelope."""
    catalog = build_catalog(payload["components"], payload.get("roots"), payload.get("prefer", "highest"))
    return Resolver(catalog).resolve()
