"""Pydantic request/response models for the resolution API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


class CandidateVersionIn(BaseModel):
    version: str = Field(..., description="严格 semver 版本号，例如 1.2.3")
    dependencies: dict[str, str] = Field(
        default_factory=dict,
        description='依赖组件名 -> semver 范围（npm 风格：^1.0.0、~1.2、">=1.0.0 <2.0.0"、1.x、*）',
    )


class ComponentIn(BaseModel):
    name: str = Field(..., min_length=1, description="组件名称")
    candidates: list[CandidateVersionIn] = Field(
        default_factory=list, description="该组件的候选版本列表（可为空，表示无可用版本）"
    )


class ResolveRequest(BaseModel):
    components: list[ComponentIn] = Field(..., min_length=1)
    roots: list[str] | None = Field(
        None, description="解析入口组件；缺省时把清单中的全部组件视为根"
    )
    prefer: Literal["highest", "lowest", "declared"] = Field(
        "highest",
        description="候选版本尝试顺序：highest=优先最高版本（默认），lowest=优先最低，declared=按提交顺序",
    )


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


class PathNodeOut(BaseModel):
    component: str
    version: str


class ConstraintInfoOut(BaseModel):
    range: str = Field(..., description="产生冲突的 semver 范围")
    required_by: PathNodeOut | None = Field(
        None, description="约束来源组件版本；null 表示根声明"
    )
    path: list[PathNodeOut] = Field(
        default_factory=list, description="从根到约束来源的依赖链"
    )


class ConflictOut(BaseModel):
    code: Literal["no_candidate", "component_not_found", "version_conflict"]
    component: str
    message: str
    minimal_constraints: list[ConstraintInfoOut] = Field(
        ..., description="不可约的最小冲突约束集（移除其中任意一条即不再直接致死）"
    )
    chains: list[str] = Field(..., description="人类可读的最小冲突链（含约束来源）")
    considered_versions: list[str] = Field(default_factory=list)
    selected_version: str | None = None
    rejected_requirement: ConstraintInfoOut | None = None


class EdgeOut(BaseModel):
    source: str
    source_version: str
    target: str
    target_version: str
    range: str


class StatsOut(BaseModel):
    nodes_visited: int
    backtracks: int
    elapsed_ms: float
    component_count: int
    candidate_count: int


class ResolutionResult(BaseModel):
    fingerprint: str
    status: Literal["satisfiable", "unsatisfiable"]
    prefer: Literal["highest", "lowest", "declared"]
    cached: bool
    hit_count: int
    created_at: str
    last_seen: str
    selection: dict[str, str] | None = None
    edges: list[EdgeOut] = Field(default_factory=list)
    conflict: ConflictOut | None = None
    stats: StatsOut


class RequestSummary(BaseModel):
    fingerprint: str
    status: str
    prefer: str
    created_at: str
    last_seen: str
    hit_count: int


class HealthOut(BaseModel):
    status: str
    database: str
