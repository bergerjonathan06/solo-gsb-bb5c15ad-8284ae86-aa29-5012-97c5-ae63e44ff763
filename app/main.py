"""FastAPI application: submit resolution, query results, idempotent reuse."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from . import db
from .config import API_PREFIX, DB_PATH, HOST, PORT
from .fingerprint import request_fingerprint
from .resolver import ResolutionInputError, resolve_payload
from .schemas import (
    HealthOut,
    RequestSummary,
    ResolutionResult,
    ResolveRequest,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    # Startup: initialise the database (directory + tables / indexes).
    db.init_db()
    yield


app = FastAPI(
    title="依赖版本兼容解析 API",
    description=(
        "提交组件清单（名称、候选版本与 semver 依赖范围），服务构建有向依赖图，"
        "用确定性回溯选择满足全部约束的版本；无解时返回最小冲突链与约束来源。\n\n"
        f"请求指纹为规范化请求体的 SHA-256，重复提交幂等复用。服务端口：**{PORT}**。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn():
    with db.connect() as conn:
        yield conn


@app.exception_handler(ResolutionInputError)
async def input_error_handler(_request, exc: ResolutionInputError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": str(exc)},
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@app.post(
    f"{API_PREFIX}/resolve",
    response_model=ResolutionResult,
    summary="提交解析（按请求指纹幂等复用）",
    status_code=status.HTTP_200_OK,
)
def submit_resolution(
    payload: ResolveRequest,
    conn=Depends(get_conn),
) -> dict:
    request_dict = payload.model_dump(exclude_none=True)
    fingerprint = request_fingerprint(request_dict)

    existing = db.get(conn, fingerprint)
    if existing is not None:
        db.touch(conn, fingerprint)
        existing = db.get(conn, fingerprint)
        return db.row_to_response(existing, cached=True)

    # Cache miss: run the (potentially expensive) search.
    result = resolve_payload(request_dict)
    db.insert_if_absent(
        conn,
        fingerprint=fingerprint,
        status=result["status"],
        prefer=request_dict.get("prefer", "highest"),
        request=request_dict,
        result=result,
    )
    # Guard against a concurrent insert with the same fingerprint.
    row = db.get(conn, fingerprint)
    return db.row_to_response(row, cached=False)


@app.get(
    f"{API_PREFIX}/resolutions/{{fingerprint}}",
    response_model=ResolutionResult,
    summary="按请求指纹查询解析结果",
)
def get_resolution(fingerprint: str, conn=Depends(get_conn)) -> dict:
    row = db.get(conn, fingerprint)
    if row is None:
        raise HTTPException(status_code=404, detail="未找到该指纹对应的解析记录")
    # A GET does not count as a new submission; keep counters untouched.
    return db.row_to_response(row, cached=True)


@app.get(
    f"{API_PREFIX}/resolutions",
    response_model=dict,
    summary="列出最近的解析请求（摘要）",
)
def list_resolutions(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn=Depends(get_conn),
) -> dict:
    rows = db.list_summaries(conn, limit, offset)
    items = [
        RequestSummary(
            fingerprint=row["fingerprint"],
            status=row["status"],
            prefer=row["prefer"],
            created_at=row["created_at"],
            last_seen=row["last_seen"],
            hit_count=row["hit_count"],
        ).model_dump()
        for row in rows
    ]
    return {"total": db.count(conn), "limit": limit, "offset": offset, "items": items}


@app.get(f"{API_PREFIX}/health", response_model=HealthOut, summary="健康检查")
def health() -> dict:
    return {"status": "ok", "database": str(DB_PATH)}


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":  # pragma: no cover
    main()
