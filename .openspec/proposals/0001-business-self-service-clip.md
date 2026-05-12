# 业务自助成片闭环（本地/OSS 视频 → 双轨道交付）

- **状态**: accepted (v3，已吸纳 Oracle 两轮评审意见)
- **创建日期**: 2026-05-12
- **作者**: IP&AI Team
- **变更记录**:
  - v1 (2026-05-12): 初稿。
  - v2 (2026-05-12): 修复 Oracle 评审 8 个 blocking 点；OSS 下载默认改 httpx；新增 JSON 原子写/并发锁、SSRF 防护、状态机完整定义、API schema 拆 oneOf、worker 启动幂等、静态资源挂载方式、清理策略、可观测性章节；验收标准全部改写为可验证条目。
  - v3 (2026-05-12): 吸纳 Oracle 复审 6 个收口意见。worker 启动改用 `lifespan=` 取代弃用的 `on_event`；`/business.html` token 流程支持 refresh（sessionStorage 优先 + meta token bootstrap）；`multipart >2GB` 清理责任明确归请求层；`3xx` 行为定性为「拒绝且 400」；补 `merge_terms(None, None, extra)` 语义；补 `schema_version` 兼容策略；验收 A5/A9/C5/C6 改写为可机器验证；限频策略备注「单进程语义」。

## 背景

当前 auto-cut 已具备完整的 AI 初剪 + 人工复核 + 千川 LLM 重排能力，但面向业务的入口只有两种：

1. CLI（`autocut run` / `autocut remix`）—— 业务无法直接使用。
2. `POST /runs` 同步 HTTP 接口 —— 跑一个长视频会阻塞数分钟到数十分钟，无法在浏览器里完整使用，且没有任何前端表单封装。
3. 视频输入只支持「本地路径」字符串，业务侧拿到的素材往往是 OSS 链接，需要先手工下载。
4. 品牌/产品/卖点 / 品牌联想词 完全靠 CLI 参数或裸 JSON，业务无法自助维护。

业务诉求：

> 「选本地视频或填 OSS 链接 → 选品牌+产品+卖点（含联想词） → 系统自动跑完 → 拿到两条成片（初剪轨道 MP4 + 千川重排 25s 短视频）」

需要在现有底座上补一条「业务自助提交 → 异步执行 → 拿成片」的闭环，并保留进入审核台手工调整的能力作为兜底。

## 目标

1. 业务可以通过 Web 表单一次性提交：视频来源（本地上传 / OSS 链接 / 白名单 URL） + 品牌（从仓库选或现填） + 产品 + 卖点 + 联想词，提交后拿到一个 `job_id`。
2. 后端在**同进程异步**跑：下载/落盘 → 初剪 pipeline → remix LLM 重排 → 双导出。业务可轮询 job 状态、查看进度、下载两条成片。
3. 提供品牌/产品仓库（本地 JSON）+ 简单 CRUD API/Web 页，支持联想词手工维护；同时支持调用 LLM 自动补全联想词候选，业务勾选后写回仓库。
4. OSS 接入复用 `F:\workCode\collect-douyin` 的同一套凭证（同 endpoint / bucket / accesskey）。**默认走 HTTP 直链下载**，仅在显式需要 SDK 鉴权场景才回退 `minio` SDK。
5. 整个对外面（Web + API）由一个简单 Bearer Token 保护；token 在前端用户输入后存 sessionStorage，所有 fetch 自动带头。
6. 现有审核台、`POST /runs`、CLI 全部保持兼容；新接口是**并列新增**，不替换。

## 非目标

- 不引入 Celery/Redis/独立 worker 进程。
- 不做多用户隔离、权限分级、登录注册（仅一个共享 token）。
- 不重写 `api.py`（52KB 已偏大，但拆分单独立项）。仅做：① 抽两个 export helper；② `include_router(business_api.router)`；③ FastAPI startup hook 启动 worker；④ 显式 `@app.get("/business.html")` 暴露页面。
- 不做品牌仓库的 SQLite / 远程同步（仅本地 JSON）。
- 不做 OSS 上传（业务素材已在 OSS 上；只下载）。
- 不做断点续跑；进程重启时活动 job 一律标 `failed (recovered)`。
- 不做分片上传；2GB 为硬上限。

## 方案

### 1. 模块拆分（新增）

```
src/autocut/
├── jobs.py           # 新增：同进程异步任务队列
├── oss.py            # 新增：URL 解析 + httpx 流式下载 + MinIO SDK fallback
├── brand_repo.py     # 新增：品牌/产品/联想词仓库（JSON + RWLock + atomic write）
├── auth.py           # 新增：Bearer token 依赖项
├── business_api.py   # 新增：业务面路由（/api/business/*），APIRouter
├── exporters.py      # 新增：从 api.py 抽出的 export helper（enabled MP4）
└── static/
    └── business.html # 新增：业务自助提交页面
data/
├── jobs/             # 新增：每个 job 的状态 + 日志
│   └── <job_id>/
│       ├── job.json
│       └── job.log
├── uploads/          # 新增：本地上传素材落盘
│   └── <job_id>/<safe_name>
└── brand_repo.json   # 新增
```

`api.py` 改动控制在 5 处（详见 §11）。

### 2. OSS / 视频源接入（`oss.py`）

#### 2.1 凭证（环境变量，复用 collect-douyin 的密钥）

| 环境变量 | 含义 | 默认值 |
|---|---|---|
| `OSS_ENDPOINT` | MinIO endpoint host（不带 scheme） | `oss.ecmax.cn` |
| `OSS_USE_SSL` | 是否启用 https | `true` |
| `OSS_ACCESS_KEY` | AccessKey | （必填，仅 SDK fallback 用） |
| `OSS_SECRET_KEY` | SecretKey | （必填，仅 SDK fallback 用） |
| `OSS_BUCKET_NAME` | 默认 bucket | `rpa` |
| `AUTOCUT_URL_ALLOWLIST` | 任意 HTTPS URL 输入允许的 host，逗号分隔 | `oss.ecmax.cn` |

#### 2.2 支持的输入形态

| 输入 type | value 示例 | 处理 |
|---|---|---|
| `upload` | （multipart 文件字段，非 JSON 路径） | 落 `data/uploads/<job_id>/<safe_name>` |
| `oss` | `oss://rpa/path/to/video.mp4` | 校验 bucket 在允许集合内；走 §2.3 |
| `url` | `https://oss.ecmax.cn/rpa/path/to/video.mp4` | 校验 host 在 `AUTOCUT_URL_ALLOWLIST`；走 §2.3 |

**只允许这三种**。其他 scheme（`ftp://` / `file://` / 等）一律 400。

#### 2.3 下载策略（默认 HTTP，SDK 为 fallback）

1. 默认：`httpx.stream("GET", url)` 流式落盘。
   - 超时：`connect=10s, read=300s`。
   - 重定向：**硬拒绝**。`follow_redirects=False`，任何 3xx 直接 400 `redirect_not_allowed`（业务侧应直接粘贴最终直链；当前 OSS bucket 为 public-read 直链，不会触发；未来若需要跟 OSS 签名跳转，再单独提案）。
   - HEAD 预检：先 HEAD 拿 `Content-Length`，>2GB 直接 400。
   - 边写边累计字节数，超过 2GB 立刻中止并删除半截文件。
2. Fallback（私有 bucket、403 等）：调用 `minio.Minio(...).get_object(bucket, key)` 流式落盘，同样的大小校验。
   - 仅当 `oss://` 输入且 HTTP 路径 403/401 时尝试一次 SDK。
3. **SSRF 防护**：
   - 解析后的 hostname 必须严格等于（或匹配）`AUTOCUT_URL_ALLOWLIST` 项；仅 host 字符串比对。
   - 解析 host → `socket.gethostbyname` 拿 IP；若 IP 落入 `127.0.0.0/8 / 10.0.0.0/8 / 172.16.0.0/12 / 192.168.0.0/16 / 169.254.0.0/16 / ::1 / fc00::/7` 一律 400。
4. **依赖**：`pyproject.toml` 新增

```toml
oss = ["minio>=7.2", "httpx>=0.27"]
```

#### 2.4 文件名安全

落盘路径统一 `data/uploads/<job_id>/source<ext>`，`ext` 仅允许 `.mp4 .mov .mkv .webm .flv .ts`，其他扩展名 400。原文件名只保存在 `job.json.request.source.original_name`，不参与文件系统。

#### 2.5 接口

```python
def resolve_video_source(
    source: dict,            # {"type": "oss|url|upload", "value": "...", "uploaded_path": "..."}
    dest_dir: Path,
    *,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> Path: ...
```

### 3. 品牌仓库（`brand_repo.py`）

#### 3.1 JSON Schema

```json
{
  "schema_version": 1,
  "brands": [
    {
      "id": "brand_pinky",
      "name": "Pinkypinky",
      "associations": ["哈利波特", "霍格沃兹"],
      "products": [
        {
          "id": "prod_xxx",
          "name": "哈利波特联名玩偶",
          "selling_points": ["正版授权", "亲肤面料"],
          "associations": ["猪猪", "厚毛绒"]
        }
      ],
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "updated_at": "..."
}
```

#### 3.2 ID 语义

- `id` 由服务端生成：`brand_` / `prod_` 前缀 + 8 字节 hex（`secrets.token_hex(4)`）；客户端不允许指定。
- `name` 在**同级**唯一（同一 brand 下产品名唯一；brand 名全局唯一），冲突返回 409。
- 允许 PATCH `name`（视为 rename），`id` 永远不变。
- DELETE brand **不级联**：若 brand 下还有 product，返回 409 `cascade_required=true`；客户端必须传 `?cascade=true` 才执行。

#### 3.2.1 `schema_version` 兼容策略

- 当前仅支持 `schema_version == 1`。
- 加载时若发现 `schema_version` 缺失或 `< 1`：视为空仓库重新初始化（首次启动场景）。
- 加载时若发现 `schema_version > 1`：**拒绝启动**，日志输出 `brand_repo.json schema_version=N > supported 1, refusing to start`，避免高版本数据被低版本服务端写坏。
- 未来变更 schema 时单独提案，并提供一次性迁移脚本 `scripts/migrate_brand_repo.py`。

#### 3.3 并发与原子性

- 进程内 `threading.RLock`（一个全局 `_REPO_LOCK`）保护内存副本读写。
- 落盘走 `atomic_write(path, data)`：先写 `path.tmp` → `os.replace(tmp, path)`，POSIX/Windows 均原子。
- 仓库 API 全部 `with _REPO_LOCK: load → mutate → atomic_write`。读多写少，性能无忧。

#### 3.4 接口

```python
def list_brands() -> list[dict]
def get_brand(brand_id: str) -> dict          # 404
def create_brand(name: str, ...) -> dict      # 409 on dup name
def update_brand(brand_id: str, patch: dict) -> dict
def delete_brand(brand_id: str, *, cascade: bool = False) -> None  # 409 if has products & not cascade

def list_products(brand_id: str) -> list[dict]
def create_product(brand_id: str, name: str, ...) -> dict  # 409 on dup name in brand
def update_product(brand_id: str, product_id: str, patch: dict) -> dict
def delete_product(brand_id: str, product_id: str) -> None

def merge_terms(brand_id: str | None, product_id: str | None,
                extra: list[str]) -> list[str]:
    """品牌联想词 + 产品联想词 + 业务表单临时追加，去重，保留先后顺序。

    语义表：
      - (brand_id=None, product_id=None)        -> 仅返回 dedupe(extra)
      - (brand_id=set,  product_id=None)        -> brand.associations + extra
      - (brand_id=set,  product_id=set)         -> brand.associations + product.associations + extra
      - (brand_id=None, product_id=set)         -> ValueError（调用方需先在 §5.3 拒绝该组合）

    去重规则：按首次出现顺序保留，大小写敏感（与 ASR hotwords 一致）。
    单测 D4 覆盖三种合法组合。
    """

def suggest_associations(brand_name: str, product_name: str,
                         selling_points: list[str]) -> list[str]:
    """调 llm.py，LLM 不可用时抛 LLMUnavailable，由路由转 502"""
```

### 4. 异步任务（`jobs.py`）

#### 4.1 完整状态机

```
                +--> partial_success
queued -> downloading -> running -> remixing -> done
                |           |          |
                +-----------+----------+--> failed
                                         (含 recovered 子状态)
```

| status | 含义 |
|---|---|
| `queued` | 入队，未开始 |
| `downloading` | 正在解析/下载视频源（含 multipart 落盘后） |
| `running` | 跑 `LiveClipPipeline.run()` |
| `remixing` | 跑 remix（仅当 `tracks` 含 `remix` 时进入） |
| `done` | 所选 tracks 全部成功 |
| `partial_success` | 双轨道任务，一条成功一条失败；artifacts 含成功那条 |
| `failed` | 整体失败 |

附加字段：

```python
@dataclass
class Job:
    id: str
    status: Literal[...]
    stage: str                      # 人读：如 "下载中 35%"
    progress: float                 # 0.0-1.0
    queued_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str
    request: dict                   # 原始 payload（已脱敏 token）
    artifacts: dict                 # {"run_id":..., "enabled_mp4":..., "remix_mp4":..., "result_json":..., "remix_plan":...}
    error: str | None
    failure_kind: Literal["download","pipeline","remix","recovered","unknown"] | None
    tracks: list[Literal["enabled","remix"]]
    tracks_result: dict             # {"enabled": "ok|failed", "remix": "ok|failed|skipped"}
```

#### 4.2 单 worker 保证

- 启动方式：使用 **`FastAPI(lifespan=...)`** 上下文管理器（`@app.on_event` 在 fastapi≥0.110 已弃用）。在 `create_app()` 里：
  ```python
  from contextlib import asynccontextmanager

  @asynccontextmanager
  async def _lifespan(app: FastAPI):
      jobs.start_worker()      # 幂等，进程级单例
      try:
          yield
      finally:
          jobs.stop_worker()   # 设 sentinel，等线程退出，最长 5s
  app = FastAPI(title="Auto Cut API", version="0.1.0", lifespan=_lifespan)
  ```
  **不**在 `create_app()` 模块加载时启 worker，**不**用 `@app.on_event("startup")`。
- 幂等保护：模块全局 `_WORKER_STARTED: threading.Event`；`start_worker()` 用 `if not _WORKER_STARTED.is_set(): _WORKER_STARTED.set(); Thread(daemon=True).start()`。
- 部署约束（写进 README + `scripts/start_review_server.ps1` 注释）：**必须** `uvicorn --workers 1 --reload 0`。多 worker / reload 场景下 worker 行为未定义；提供 `AUTOCUT_DISABLE_WORKER=1` 给单元测试和 dev 调试用。
- worker 线程跑同步 `pipeline.run()` 时不会阻塞 anyio threadpool；但承认会与同步路由抢 GIL/CPU（性能而非正确性问题）。

#### 4.3 并发与原子性

- `job.json` 写入走 `atomic_write`。每次状态推进一次写一次。
- `job.log` 走 `with _JOB_LOCKS[job_id]: f.write(line)`，append 模式；log 读接口用 `tail -n` 等价 Python 实现。
- queue 用 `queue.Queue`，提交线程 put，worker get。

#### 4.4 进程重启恢复

- 启动时扫 `data/jobs/*/job.json`，所有非 `done/failed/partial_success` 的 job 直接改写为：
  ```json
  {"status": "failed", "failure_kind": "recovered",
   "error": "recovered after restart", "finished_at": "<now>"}
  ```
- 同时往 `job.log` 追一行 `[RECOVERED] worker did not finish before restart`。

#### 4.5 失败语义

- 下载阶段失败 → `failed`, `failure_kind="download"`。
- pipeline 失败 → `failed`, `failure_kind="pipeline"`。
- remix 失败：
  - 若 `tracks=["remix"]`：`failed`, `failure_kind="remix"`。
  - 若 `tracks=["enabled","remix"]`：`partial_success`, `failure_kind="remix"`, `tracks_result={"enabled":"ok","remix":"failed"}`, artifacts 含 `enabled_mp4`。

### 5. 业务 API（`business_api.py`）

所有路由前缀 `/api/business`，统一 `Depends(require_token)`（详见 §6）。

#### 5.1 路由总表

| Path | Method | 用途 | 主要状态码 |
|---|---|---|---|
| `/jobs` | POST | 提交任务（JSON 或 multipart） | 202 / 400 / 401 / 413 / 422 |
| `/jobs` | GET | 列表（最近 50，含 `queue_position`） | 200 / 401 |
| `/jobs/{job_id}` | GET | 详情 | 200 / 401 / 404 |
| `/jobs/{job_id}/log` | GET | tail 日志（`?n=200`） | 200 / 401 / 404 |
| `/jobs/{job_id}/artifact/{kind}` | GET | 下载产物 | 200 / 401 / 404 / 409 (未就绪) |
| `/brands` | GET / POST | 列表 / 新建 | 200,201 / 400 / 401 / 409 |
| `/brands/{id}` | GET / PATCH / DELETE | CRUD | 200,204 / 400 / 401 / 404 / 409 |
| `/brands/{id}/products` | GET / POST | 列表 / 新建 | 200,201 / 400 / 401 / 404 / 409 |
| `/brands/{bid}/products/{pid}` | GET / PATCH / DELETE | CRUD | 200,204 / 401 / 404 / 409 |
| `/brands/{id}/suggest-associations` | POST | LLM 联想词建议 | 200 / 401 / 404 / 502 (LLM 不可用) |
| `/metrics` | GET | 观测指标（见 §10） | 200 / 401 |

#### 5.2 `POST /api/business/jobs` 严格 schema

**Content-Type 决定调用形态，二选一**：

**A. `application/json`**（用于 OSS / URL 输入）：

```json
{
  "source": {
    "type": "oss",                                  // 必填，枚举 "oss" | "url"
    "value": "oss://rpa/path/to/video.mp4"          // 必填，且与 type 匹配
  },
  "product": {
    "brand_id": "brand_pinky" | null,
    "product_id": "prod_xxx" | null,
    "product_name": "..." | null,
    "selling_points": ["..."],
    "extra_associations": ["..."]
  },
  "tracks": ["enabled", "remix"],                   // 必填，非空，元素 ∈ {"enabled","remix"}
  "remix_target_duration": 25.0,                    // 可选，默认 25.0
  "asr": {                                          // 可选
    "engine": "faster-whisper",
    "model": ".\\models\\faster-whisper-small",
    "device": "cpu",
    "compute_type": "int8"
  }
}
```

**B. `multipart/form-data`**（用于本地上传）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `file` | 是 | 视频文件 |
| `payload` | 是 | 一个 JSON 字符串，**不含 `source`**，其余字段同 A |

后端自动注入 `source = {"type":"upload","uploaded_path":"..."}`。

#### 5.3 product 字段必填组合

校验规则（违反返回 422）：

- `tracks` 必须非空且只能含 `enabled` 和/或 `remix`。
- `product` 二选一组合：
  - **仓库路径**：`brand_id` 必填 AND `product_id` 必填；`product_name`/`selling_points`/`extra_associations` 可选（缺失时从仓库取）。
  - **裸名路径**：`brand_id`/`product_id` 均为 null AND `product_name` 必填 AND `selling_points` 非空。
- 任何混合（`brand_id` 给了但 `product_id` 没给 / 给了 `brand_id` 但仓库没这条记录）→ 422。
- `selling_points` 最终拼好的列表非空（仓库取出或裸名手填），否则 422。

后端把校验好的 product 信息转换为 `PipelineRequest`：

```python
PipelineRequest(
    video_path=resolved_local_path,
    product=product_name,
    selling_points=selling_points_merged,
    brand_terms=brand_repo.merge_terms(brand_id, product_id, extra_associations),
    use_default_brand_terms=True,
    asr_engine=asr.engine,
    # ... 其余沿用默认
)
```

#### 5.4 错误码语义表

| 场景 | HTTP |
|---|---|
| 未带或错 token | 401 |
| schema 校验失败 / 必填缺失 / 字段冲突 | 422 |
| URL host 不在 allowlist / 私有 IP / `oss://` bucket 不允许 | 400 |
| 远端 HEAD 失败 / 404 | 400（带 `detail.upstream_status`） |
| Content-Length > 2GB | 400（HEAD 阶段）/ 413（multipart 阶段） |
| job_id / brand_id / product_id 不存在 | 404 |
| artifact 还没产出 | 409 `not_ready` |
| brand 删除时还有 products | 409 `cascade_required=true` |
| 重名 | 409 `duplicate_name` |
| LLM 上游不可用 | 502 |
| 限频超限 | 429 |

### 6. 鉴权（`auth.py`）

- 启动时读 `AUTOCUT_API_TOKEN`（必填）；未设置时启动失败并打印明确日志。
- `require_token`：从 `Authorization: Bearer <token>` 校验，**也接受** `X-Autocut-Token: <token>`（前端 fetch 用），**也接受** `Cookie: autocut_token=<token>`（专给页面 refresh 用，见下）。
- 保护范围：
  - `/api/business/*` 全部。
  - `/business.html` **本身也校验**：通过显式路由 `@app.get("/business.html")` 实现。
  - 现有 `/api/runs/*`、`/`、`/health` **不变**（保持本地审核台体验）。

#### 6.1 页面 token bootstrap（支持 refresh）

业务首次访问 `/business.html?token=<TOKEN>` 时：

1. 后端 `@app.get("/business.html")` 校验 query 里的 `token`：
   - 缺失或错 → 返回 401 + 极简 HTML：「请在 URL 加 `?token=...` 访问」。
   - 正确 → `Set-Cookie: autocut_token=<TOKEN>; Path=/; HttpOnly=false; SameSite=Strict`（HttpOnly 设 false 是为了让 JS 也能读，用于 fetch header；不引入 XSS 是因为页面不渲染用户输入 HTML）。
2. 返回的 HTML 内 `<meta name="autocut-token" content="<TOKEN>">`，JS 启动时同步写一份到 `sessionStorage["autocut_token"]`。
3. JS 加载完成后 `history.replaceState(null, "", "/business.html")` 清掉 URL 里的 `?token=`。
4. 后续所有 fetch 都自动带 `X-Autocut-Token` header（值来自 `sessionStorage`）。

**Refresh 行为**：浏览器对 `/business.html` 的 refresh 请求自动带 `Cookie: autocut_token=<TOKEN>`，路由认 Cookie → 200，页面继续工作。这条路径不依赖 sessionStorage 也不依赖 URL query。

**关闭浏览器后再开**：sessionStorage 已清，Cookie 默认是 session cookie 也会清；业务需重新走 `?token=` 一次。这是接受的取舍（避免 token 永驻磁盘）。

**错 token 行为**：`?token=<WRONG>` 一律 401，**不**写 cookie；refresh 仍是 401。

#### 6.2 静态资源策略

CSS/JS 全部内嵌在 `business.html` 单文件里，**不挂 StaticFiles**，避免引入更大的鉴权面。

### 7. 前端（`static/business.html`）

单页，与现有 `static/index.html` 风格一致（vanilla HTML/CSS/JS，无构建链路）。三个 Tab：

1. **提交任务**：视频来源 Radio（本地上传 / OSS 或 URL） → 品牌下拉（含「+ 新建」） → 产品下拉 → 卖点（textarea，每行一条） → 联想词（chip 编辑器 + 「LLM 推荐」按钮）→ tracks 复选框（默认两条都勾） → ASR 高级设置（折叠） → 「提交」按钮。
2. **任务列表**：表格 + 状态徽标 + 进度条 + 「打开审核台」「下载初剪」「下载 remix」「查看日志」按钮；2 秒轮询活动任务。
3. **品牌仓库**：表格式 CRUD + 联想词 chip 编辑 + 「LLM 推荐」按钮（结果勾选写回）。

页面通过显式路由 `@app.get("/business.html")` 暴露（见 §6），不依赖 StaticFiles 挂载。

### 8. 与现有审核台衔接

业务任务完成后产物落标准 `data/runs/<run_id>/`，`job.artifacts.run_id` 必填，前端「打开审核台」直接跳 `/?run=<run_id>`。审核台现成路由全部可用。

### 9. Pipeline / Export 复用

**PR-3 包含一次小重构**：把 `api.py` 中 `export_run_timeline`（行 211-243）的核心逻辑抽到 `exporters.py`：

```python
def export_enabled_timeline(run_dir: Path, run_id: str) -> dict:
    """返回 {path,url,ranges,duration}，缺前置条件抛 ValueError；现有路由也改调它。"""
```

worker 直接调 `exporters.export_enabled_timeline` 和已存在的 `export_remix_plan`。

### 10. 观测与运维

- **结构化日志**：每条 `job.log` 行包含 `{"ts": ISO, "level": "...", "job_id": "...", "stage": "...", "msg": "..."}`。worker 用统一 `log(job_id, level, stage, msg)`。
- **指标**（`GET /api/business/metrics`，受 token 保护）：`queue_length`、`active_workers`（恒 1）、最近 50 个 job 的 `success_rate` / `mean_duration_seconds` / `failure_kind_breakdown`。
- **限频**：`POST /api/business/jobs` 全局 ≤ 10 job/min（内存计数 + 滑动窗口）；`POST /api/business/brands/{id}/suggest-associations` ≤ 5 req/min。超限 429。**注意**：内存滑窗仅在「单进程 + 无 reload」生产语义下有效；dev `--reload` 或 worker 重启会重置计数。这是接受的代价（避免引入 Redis）。
- **数据隐私**：`job.request` 落盘前移除 `asr.api_key`-类字段；`job.log` 不打印视频内容。
- **清理策略**：新增 `scripts/cleanup_old_jobs.ps1`，默认保留最近 14 天的 `data/uploads/<job_id>/` 与 `data/jobs/<job_id>/`；写进 README，业务环境用计划任务跑。配额由运维侧落盘前检查。
- **i18n**：MVP 仅中文 UI；明确写进 README，不留歧义。

### 11. 对 `api.py` 的精确改动清单

| # | 改动 | 行为 |
|---|---|---|
| 1 | `app.include_router(business_api.router)` | 注册新路由组 |
| 2 | `FastAPI(lifespan=_lifespan)` 启动 worker（`lifespan` 上下文管理器，取代弃用的 `@app.on_event`） | 启动 worker（幂等） + 退出时 stop_worker |
| 3 | `@app.get("/business.html")` | 显式暴露页面（带 token 校验） |
| 4 | `export_run_timeline` 改为调 `exporters.export_enabled_timeline` | 抽 helper 复用 |
| 5 | 新增 `_safe_run_dir` 内部错误码统一（不变 API 兼容） | 仅为新模块复用 |

不删除、不改造任何现有路由签名。

## 验收标准（全部可验证）

### A. 输入与下载

A1. **OSS 输入**（`source.type=oss`，`value=oss://rpa/<已存在 key>`）：返回 202 + `job_id`；轮询至 `status=done`，`artifacts.enabled_mp4` 文件存在。

A2. **HTTPS 直链白名单内**：`https://oss.ecmax.cn/rpa/<key>` 同上成功。

A3. **白名单外**：`https://example.com/x.mp4` 返回 400 `host_not_allowed`。

A4. **bucket 不允许**：`oss://other-bucket/x.mp4` 返回 400 `bucket_not_allowed`。

A5. **私有 IP（机器验证）**：单测 monkeypatch `socket.gethostbyname` 返回 `127.0.0.1`，再调下载入口 → 抛 `PrivateAddressBlocked`；HTTP 路由返回 400 `private_address_blocked`。同样覆盖 `10.0.0.1`、`192.168.1.1`、`::1`、`fc00::1`、`169.254.169.254`。

A6. **远端 404**：HEAD 返回 404 → 业务接口 400，`detail.upstream_status=404`。

A7. **远端 >2GB**：HEAD `Content-Length=3000000000` → 400 `too_large`，本地无残留文件。

A8. **multipart 上传 ≤2GB**：成功 202；落盘 `data/uploads/<job_id>/source.mp4`。

A9. **multipart 上传 >2GB**：请求处理链在解析 multipart 时累计 body 字节，**超过 2GB 立刻中止读取**并返回 413；同一函数在 return 前 `shutil.rmtree(data/uploads/<job_id>/, ignore_errors=True)`，保证该目录不残留任何文件。**清理责任在请求层，不依赖 worker**（该 job 从未入队）。验收断言：`data/uploads/<job_id>/` 不存在或为空。

A10. **非法 scheme**（`ftp://...` / `file://...`）：400 `scheme_not_allowed`。

A11. **非法扩展名**：上传 `x.exe` 或 URL 指向 `.exe`：400 `extension_not_allowed`。

A12. **3xx 拒绝（机器验证）**：单测用 `httpx.MockTransport` 让 `https://oss.ecmax.cn/rpa/x.mp4` 的 HEAD 返回 302 → 业务接口返回 400 `redirect_not_allowed`，本地无残留文件。

### B. 任务状态机

B1. **enabled-only**（`tracks=["enabled"]`）：状态推进 `queued → downloading → running → done`，**不进入** `remixing`。

B2. **remix-only**（`tracks=["remix"]`）：`queued → downloading → running → remixing → done`，且 `done` 后 `artifacts.remix_mp4` 存在、`artifacts.enabled_mp4` 缺失。

B3. **双轨道全成**：`done`，`tracks_result={"enabled":"ok","remix":"ok"}`，两个 MP4 都存在。

B4. **双轨道 remix 失败**（mock LLM 502）：`partial_success`，`tracks_result={"enabled":"ok","remix":"failed"}`，`enabled_mp4` 存在，`failure_kind="remix"`。

B5. **pipeline 失败**（坏视频文件）：`failed`，`failure_kind="pipeline"`。

B6. **下载失败**：`failed`，`failure_kind="download"`。

B7. **进程重启恢复**：写一个 `status=running` 的假 job 然后启动服务，启动后该 job → `status=failed`, `failure_kind=recovered`, `error="recovered after restart"`；新提交 job 正常进入队列并完成。

### C. 鉴权与页面暴露

C1. `curl /api/business/jobs` 无 header → 401。

C2. `curl -H "Authorization: Bearer wrong" /api/business/jobs` → 401。

C3. `curl -H "Authorization: Bearer <correct>" /api/business/jobs` → 200。

C4. `curl -H "X-Autocut-Token: <correct>" /api/business/jobs` → 200。

C5a. 浏览器访问 `/business.html?token=<correct>` → 200，HTML 返回；响应含 `Set-Cookie: autocut_token=<correct>`；HTML body 含 `<meta name="autocut-token" content="<correct>">`。

C5b. **Refresh**：在 C5a 之后，模拟带 `Cookie: autocut_token=<correct>` 的 `GET /business.html`（无 `?token=` query） → 200。

C5c. 浏览器访问 `/business.html?token=<wrong>` → 401；响应**不**带 `Set-Cookie`；body 是简易提示 HTML。

C6. 浏览器访问 `/business.html` 无 token 无 cookie → 401 + 简易提示页。

C7. `curl /api/runs` 不带 token → 200（兼容现状）。

### D. 品牌仓库

D1. `POST /brands` 重名 → 409 `duplicate_name`。

D2. `DELETE /brands/{id}` 有产品 → 409 `cascade_required=true`；`?cascade=true` 后 204。

D3. PATCH `name`：`name` 改了，`id` 不变；GET 取回字段一致。

D4. `merge_terms` 单测：品牌联想词 ["A","B"] + 产品联想词 ["B","C"] + extra ["D","A"] → `["A","B","C","D"]`。

D5. **并发写不损坏 JSON**：测试中起 20 个线程各 POST 一个 brand，最终 `brand_repo.json` 可正常 `json.load`，且包含全部 20 条。

D6. **LLM 推荐**：mock llm 返回 8 词 → 200 + 8 词；真实 llm 503 → 502 `upstream_unavailable`。

### E. 衔接审核台

E1. job `done` 后 `GET /api/runs/{artifacts.run_id}` 返回 200，包含 candidates。

E2. 在审核台禁用一个 piece → `POST /api/runs/{id}/export` 200 → 新 enabled.mp4 时长变短。

### F. 工程

F1. `pip install -e ".[api,asr,oss]"` 一行装齐，无错误。

F2. `python -m unittest discover` 全部 pass；至少含 `test_oss.py`、`test_brand_repo.py`、`test_jobs.py`、`test_business_api.py`、`test_exporters.py`。

F3. `ruff check src/autocut` 干净。

F4. `README.md` 含「业务自助成片」章节，列出全部 env var、`uvicorn --workers 1` 限制、token 设置方式。

## 风险与权衡

| 风险 | 缓解 |
|---|---|
| 同进程 worker 与同步路由抢 GIL/CPU，业务轮询接口在 pipeline 运行时变慢 | 接受；MVP 阶段单业务并发；如成为瓶颈，下一期再上 Celery/RQ |
| `uvicorn --workers > 1` 时多 worker 启动副本 | 文档强约束 + 启动脚本写死 `--workers 1`；提供 `AUTOCUT_DISABLE_WORKER=1` 给 dev 用 |
| 大文件上传中前端断网 | 接受；本期不做断点续传；超时后业务重传 |
| MinIO SDK 在 Python 3.13 兼容性 | 提案默认 HTTP 路径不依赖 SDK；SDK fallback 在 try/except 内引入，缺包不致命 |
| SSRF allowlist 维护成本 | env var 配置，运维侧统一 |
| LLM 调用突增 | suggest-associations 限频 5/min；prompt 控长以保证 ≤10s 通常返回 |

## 实施拆分（按顺序串行合并）

> 五个 PR **串行依赖**（非独立），每个都能单独 Code Review。

1. **PR-1 基础设施**：`auth.py` + `brand_repo.py` + `business_api.py` 骨架 + 品牌 CRUD 路由 + `data/brand_repo.json` 初始化 + 鉴权单测 + CRUD 单测 + 并发写单测。
2. **PR-2 视频源接入**：`oss.py`（含 SSRF/大小/allowlist 校验）+ `pyproject.toml [oss]` extras + 单测（httpx mock + minio mock）。
3. **PR-3 Export Helper 抽取**：`exporters.py` + 改 `api.py` 内联实现调 helper + 现有审核台行为完全不变的回归测试。
4. **PR-4 异步任务 + 端到端 job**：`jobs.py` + worker startup hook + `POST/GET /api/business/jobs` + 进程重启恢复逻辑 + 集成测试（mock pipeline 短跑通）。
5. **PR-5 前端 + 联想词 + 观测**：`static/business.html` + `/business.html` 路由 + `suggest-associations` + `/metrics` + 限频中间件 + `cleanup_old_jobs.ps1` + README 更新。

## 备注

- v2 在 v1 基础上吸纳 Oracle 评审 8 个 blocking + 6 个 should-fix；nice-to-have 中的 `queued_at/started_at/finished_at` 与 `queue_position` 已并入主方案。
- 后续若 `api.py` 拆分（按 runs / timeline / remix / business 四 router），单独立项。
- 业务素材未来可能从「OSS 直链」升级为「内部素材中台 API」：扩展 `resolve_video_source` 一个函数即可。
