# 依赖版本兼容解析 API

用 **Python / FastAPI / SQLite** 构建的依赖版本兼容解析服务：客户端提交组件清单
（组件名称、候选版本、各版本的 semver 依赖范围），服务构建有向依赖图，并通过
**确定性回溯（deterministic backtracking）** 选出满足全部约束的版本组合；
无解时返回**最小冲突链**与每条约束的**来源**。相同的解析请求按其 **SHA-256 指纹**
幂等复用，无需重复计算。

## 功能特性

- **Semver 约束**：基于 [`semantic-version`](https://pypi.org/project/semantic-version/)
  的 NPM 风格范围，支持 `^1.2.3`、`~1.2`、`>=1.0.0 <2.0.0`、`1.x`、`*` 等。
- **有向依赖图 + 确定性回溯**：
  - MRV（最少剩余值）启发式选择下一个待赋值组件，平局按组件名排序；
  - 候选版本按固定顺序尝试（`highest` 默认 / `lowest` / `declared`）；
  - 任何输入都有唯一、可复现的结果（含无解时的冲突报告）。
- **最小冲突链**：无解时在所有死点中取最深死点，并对约束集做不可约化简
  （移除其中任意一条约束后即存在候选版本可满足），输出：
  - `minimal_constraints`：最小冲突约束集及每条约束的来源组件版本、从根出发的依赖路径；
  - `chains`：人类可读的冲突链（含约束来源）；
  - `considered_versions` / `rejected_requirement`：被否候选与致命约束。
- **请求指纹幂等**：对请求做规范化（图无关节点顺序归一；`prefer != declared`
  时候选版本按 semver 排序归一）后取 SHA-256。重复提交直接复用 SQLite 中的结果并
  累加命中次数；`GET` 查询不改变计数。
- **启动即初始化**：容器启动时先执行数据库初始化（建目录、建表、建索引），再对外服务。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── config.py          # 端口 / 数据库路径等配置
│   ├── schemas.py         # Pydantic 请求/响应模型
│   ├── resolver.py        # 依赖图构建 + 确定性回溯 + 最小冲突链
│   ├── fingerprint.py     # 请求规范化与 SHA-256 指纹
│   ├── db.py              # SQLite 初始化与存取
│   ├── init_db.py         # 数据库初始化 CLI（容器启动入口调用）
│   └── main.py            # FastAPI 路由
├── tests/                 # pytest 单元 + 接口测试
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh   # 先初始化数据库，再启动 uvicorn
├── requirements.txt
└── requirements-dev.txt
```

## 快速开始（Docker Compose，推荐）

```bash
docker compose up --build
```

服务监听 **8000 端口**（宿主机 `8000` → 容器 `8000`），数据库持久化在命名卷
`resolver-data` 内（`/data/resolver.db`）。

- API 地址：`http://localhost:8000`
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/api/v1/health>

修改端口映射请编辑 `docker-compose.yml` 中的 `ports`（容器内端口由环境变量
`PORT` 控制，默认 `8000`，`Dockerfile` 已 `EXPOSE 8000`）。

## 本地运行

需要 Python 3.11+。

```bash
python -m pip install -r requirements.txt

# 1) 初始化依赖与数据库（生成 ./data/resolver.db）
python -m app.init_db

# 2) 启动服务（默认 0.0.0.0:8000）
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或：python -m app.main
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 服务监听端口 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `RESOLVER_DB` | `./data/resolver.db` | SQLite 数据库文件路径（容器内为 `/data/resolver.db`） |

> 注意：直接 `uvicorn app.main:app` 启动时，应用的 lifespan 也会自动初始化数据库；
> `docker-entrypoint.sh` 中的 `python -m app.init_db` 使初始化在日志中显式可见、
> 并在服务接收流量前完成。

## API

所有接口前缀：`/api/v1`

### 1. 提交解析（幂等）

`POST /api/v1/resolve`

请求体：

```json
{
  "components": [
    {
      "name": "app",
      "candidates": [
        {"version": "1.0.0", "dependencies": {"web": "^2.0.0", "util": ">=1.0.0 <3.0.0"}}
      ]
    },
    {
      "name": "web",
      "candidates": [
        {"version": "2.1.0", "dependencies": {"util": "~1.5.0"}},
        {"version": "2.0.0", "dependencies": {"util": "~1.4.0"}}
      ]
    },
    {"name": "util", "candidates": [{"version": "1.4.2"}, {"version": "1.5.1"}, {"version": "3.0.0"}]}
  ],
  "roots": ["app"],
  "prefer": "highest"
}
```

- `roots` 可选，缺省表示清单中的全部组件都是根；从任何根不可达的组件不参与解析。
- `prefer`：`highest`（默认，优先高版本）、`lowest`（优先低版本）、
  `declared`（按提交顺序尝试）。

成功响应（节选）：

```json
{
  "fingerprint": "b570515e…",
  "status": "satisfiable",
  "cached": false,
  "hit_count": 1,
  "selection": {"app": "1.0.0", "web": "2.1.0", "util": "1.5.1"},
  "edges": [
    {"source": "web", "source_version": "2.1.0", "target": "util",
     "target_version": "1.5.1", "range": "~1.5.0"}
  ],
  "stats": {"nodes_visited": 3, "backtracks": 0, "elapsed_ms": 0.57,
            "component_count": 3, "candidate_count": 6}
}
```

再次提交语义相同的请求（即使组件、候选、依赖键的排列顺序不同），响应为
`"cached": true`，`hit_count` 递增，`fingerprint` 不变。

### 2. 无解：最小冲突链

`POST /api/v1/resolve`（`app` 要求 `lib ^2.0.0`，`plugin` 要求 `lib ~1.2.0`）：

```json
{
  "status": "unsatisfiable",
  "fingerprint": "8c583333…",
  "conflict": {
    "code": "no_candidate",
    "component": "lib",
    "message": "组件 'lib' 的 2 个候选版本中，没有任何版本同时满足最小冲突约束集",
    "minimal_constraints": [
      {"range": "^2.0.0", "required_by": {"component": "app", "version": "1.0.0"},
       "path": [{"component": "app", "version": "1.0.0"}]},
      {"range": "~1.2.0", "required_by": {"component": "plugin", "version": "3.0.0"},
       "path": [{"component": "plugin", "version": "3.0.0"}]}
    ],
    "chains": [
      "app@1.0.0 -> 要求 lib ^2.0.0（约束来源: app@1.0.0）",
      "plugin@3.0.0 -> 要求 lib ~1.2.0（约束来源: plugin@3.0.0）"
    ],
    "considered_versions": ["2.0.0", "1.2.9"],
    "rejected_requirement": {"range": "~1.2.0", "...": "…"}
  }
}
```

冲突类型 `conflict.code`：

| code | 含义 |
| --- | --- |
| `no_candidate` | 该组件的所有候选版本都无法同时满足最小冲突约束集 |
| `version_conflict` | 组件已选定版本后出现不兼容约束，且尝试其他赋值仍无解 |
| `component_not_found` | 约束指向清单中不存在的组件 |

### 3. 按指纹查询结果

`GET /api/v1/resolutions/{fingerprint}` → 返回与提交时一致的结果（`cached: true`）；
不存在返回 `404`。GET 不增加 `hit_count`。

### 4. 历史列表

`GET /api/v1/resolutions?limit=50&offset=0` → 按最近使用时间倒序返回摘要
（指纹、状态、prefer、时间戳、命中次数）。

### 5. 健康检查

`GET /api/v1/health` → `{"status": "ok", "database": "/data/resolver.db"}`

## 解析算法说明

1. **建图**：每个候选版本是一个潜在节点，其 `dependencies` 以带来源信息的
   semver 范围标注；根组件默认附带 `*` 约束。
2. **搜索**：深度优先回溯。每一步用 MRV 选择"可行候选最少"的未赋值组件
   （平局按名称），再按确定的版本序逐一尝试，并把该版本声明的依赖约束传播给
   相邻组件；若传播直接撞上已赋值组件则提前剪枝。
3. **取证**：每个死点记录当前约束、被否约束、考虑过的版本；搜索失败后取
   **最深死点**（平局按组件名与约束签名排序，保证确定性）。
4. **不可约化**：对死点约束集做贪心删除——删去某条约束后若重新变得（相对已知
   候选）不可满足，则保留该约束；得到的最小集合即最小冲突约束集，配合来源路径
   生成冲突链。

该不可约集合是"约束级"最小集：其中每条约束都对无解有贡献。它是按确定性顺序
贪心得到的最小杀手集（在最小性上稳定、可复现），并非所有可能极小集的枚举。

## 测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

覆盖：有解选择与 `prefer` 策略、强制回溯、菱形依赖、确定性重复解析、
三类无解冲突与最小约束集、非法 semver/范围/重复组件的 422、指纹幂等与
乱序归一、指纹查询 404、历史列表等。

## 使用 cURL 快速验证

```bash
curl -s http://localhost:8000/api/v1/health
curl -s -X POST http://localhost:8000/api/v1/resolve \
  -H 'Content-Type: application/json' \
  -d '{"components":[{"name":"a","candidates":[{"version":"1.0.0","dependencies":{"b":"^1.0.0"}}]},{"name":"b","candidates":[{"version":"1.0.0"},{"version":"1.5.0"}]}]}'
```
