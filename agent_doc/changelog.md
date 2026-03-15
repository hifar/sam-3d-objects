# Changelog

---

## 2026-03-15 — 新增 TestMode 配置化 Mock 推理流程

### 新增文件

#### `app_config.json`（新建）
- 新增应用级配置文件，默认内容：

```json
{
  "TestMode": false,
  "MockDataDir": "Test",
  "MockPlyFile": "mockup.ply",
  "MockGlbFile": "mockup.glb",
  "MockSleepSeconds": 10
}
```

- 该文件用于控制 Worker 是否进入 mock 测试模式。

---

#### `api/app_config.py`（新建）
- 新增 `AppConfig` dataclass，定义应用配置结构：
  - `test_mode`
  - `mock_data_dir`
  - `mock_ply_file`
  - `mock_glb_file`
  - `mock_sleep_seconds`
- 新增 `load_app_config()`：
  - 默认从项目根目录 `app_config.json` 读取。
  - 支持通过环境变量 `APP_CONFIG_FILE` 覆盖路径。
  - 文件不存在时回退到内置默认值（`TestMode=False`）。

---

### 修改文件

#### `api/worker.py`

1. 新增配置接入
- 引入 `load_app_config()` 并在模块加载时初始化 `_app_config`。

2. 新增 TestMode 辅助逻辑
- 新增 `_resolve_mock_path(base_dir, file_name)`：
  - 兼容绝对路径和相对路径。
  - 相对路径按仓库根目录解析。
- 新增 `_run_test_mode_job(job_id, outputs_dir)`：
  - 更新 `progress_stage=test_mode_sleep`
  - 按 `MockSleepSeconds` 执行 `time.sleep(...)`
  - 检查 mock 文件存在性（`mockup.ply` / `mockup.glb`）
  - 更新 `progress_stage=copying_mock_artifacts`
  - 复制到输出目录：
    - `outputs/splat.ply`
    - `outputs/mesh.glb`

3. `_run_job(job_id)` 分支改造
- 在真实推理前增加 TestMode 分支：
  - 若 `TestMode=True`：执行 mock 流程，标记 `SUCCEEDED`，并 `return`。
  - 若 `TestMode=False`：保持原真实推理流程不变。

4. 新增依赖导入
- 增加 `shutil`、`time` 导入用于文件复制和延时模拟。

---

### 行为变化（对 API 影响）

- 当 `app_config.json` 中 `TestMode=true` 时：
  - `POST /v1/jobs` 提交后，Worker 不调用模型推理。
  - 任务约 10 秒后完成（可配置）。
  - 下载接口返回的是 `Test/` 目录下 mock 文件复制后的结果。
- 当 `TestMode=false` 时：
  - 行为与之前一致，执行真实 GPU 推理。

---

## 2026-03-15 — 初始 FastAPI REST API 服务搭建

### 新增文件

#### `main.py`
- 从空文件改写为 Uvicorn 启动入口。
- 使用 `api.create_app()` 组合 FastAPI 应用实例。
- 设置 `reload=False`，防止 Uvicorn 热重载复制后台 Worker 线程。

```python
import uvicorn
from api import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
```

---

#### `api/__init__.py`（新建）
- 定义 `create_app()` 工厂函数，返回配置好的 `FastAPI` 实例。
- 通过 `@asynccontextmanager` 实现 `lifespan`，在应用启动时调用 `start_worker()`，在关闭时调用 `shutdown_worker()`，确保后台线程生命周期与应用完全绑定。
- 将 `api.routes.router` 以 `/v1` 前缀挂载到 app。

---

#### `api/models.py`（新建）
- `JobStatus`：枚举类，定义任务生命周期状态：`queued` / `running` / `succeeded` / `failed` / `canceled`。
- `JobRecord`：Python `dataclass`，存储任务的全部内部状态（id、状态、时间戳、输入输出目录、错误信息等）。
- `JobOut`：Pydantic 响应模型，用于任务状态 API 的 JSON 序列化输出，包含 `queue_position`、`progress_stage`、`duration_ms` 等字段。
- `JobListOut`：任务列表分页响应模型。
- `ArtifactInfo` / `JobResultOut`：产物清单响应模型，含下载 URL 和文件大小。

---

#### `api/store.py`（新建）
- `InMemoryJobStore`：基于 `threading.Lock` 的线程安全内存字典存储。
- 提供 `add()`、`get()`、`update(**kwargs)`、`list_all()` 四个方法。
- `list_all()` 支持按 `JobStatus` 过滤和分页（`page` / `page_size`），按创建时间倒序排列。
- 模块级单例 `job_store` 供全局共享。

---

#### `api/worker.py`（新建）
- 后台单线程 Worker，从 `queue.Queue` 消费任务，严格串行执行（单 GPU 安全）。
- **推理模型懒加载**：首次消费任务时才加载模型，避免 API 启动慢；通过 `threading.Lock` 保证只加载一次。
- `CONDA_PREFIX` 回退处理：uv 环境未设置 `CONDA_PREFIX` 时自动填充，防止 `notebook/inference.py` 模块级赋值报错。
- 推理步骤分阶段更新 `progress_stage`：`preprocessing` → `running_stage1` → `saving_ply` → `saving_mesh` → `done`。
- `submit(job_id)`：入队并返回当前队列长度（近似排队位置）。
- `cancel(job_id)`：将排队中的任务标记为 `canceled` 并加入 `_cancel_set`；Worker 在取出任务后先检查取消集合，实现软取消。
- `queue_position(job_id)`：返回任务在队列中的当前 1-based 位置。
- `start_worker()` / `shutdown_worker()`：由 lifespan 钩子调用，通过 `_stop_event` 和哨兵值 `None` 优雅停止线程。
- 推理产物保存逻辑：
  - 必须：`outputs/splat.ply`（通过 `GaussianModel.save_ply()`）
  - 可选：`outputs/mesh.glb`（当 `generate_mesh=True` 时导出）
- 异常时将任务状态置为 `failed`，错误信息截断至 500 字符后写入 `JobRecord.error_message`（防止内部堆栈泄露）。
- 配置通过环境变量注入：`SAM3D_CONFIG_FILE`、`SAM3D_STORAGE_ROOT`。

---

#### `api/routes.py`（新建）
全部 REST 端点实现，路由前缀 `/v1`：

| 端点 | HTTP 状态码 | 关键逻辑 |
|------|-------------|---------|
| `POST /v1/jobs` | 202 | 校验 content-type（PNG/JPEG/WebP）和文件大小（≤ 20 MB）；按 UUID 创建隔离目录；入队后立即返回 |
| `GET /v1/jobs` | 200 | 调用 `store.list_all()`，支持 `?status=` 过滤和 `page` / `page_size` 分页 |
| `GET /v1/jobs/{job_id}` | 200 / 404 | 返回 `JobOut`，包含实时队列位置和已计算耗时 |
| `DELETE /v1/jobs/{job_id}` | 200 / 409 | 仅 `queued` 可取消；`running` 返回 409 |
| `GET /v1/jobs/{job_id}/result` | 200 / 404 / 409 | 仅 `succeeded` 时返回清单；扫描 outputs 目录补充文件大小 |
| `GET /v1/jobs/{job_id}/artifacts/ply` | 200 / 404 / 409 | `FileResponse` 流式下载，Content-Type: `application/octet-stream` |
| `GET /v1/jobs/{job_id}/artifacts/mesh_glb` | 200 / 404 / 409 | `FileResponse` 流式下载，Content-Type: `model/gltf-binary` |
| `GET /v1/health` | 200 | 返回 UTC 时间戳 |

- 全局辅助函数 `_require_job()` 和 `_require_succeeded()` 统一处理 404 / 409 错误，避免重复代码。
- 请求参数校验基于 FastAPI `File`/`Form`/`Query` 注解完成，不引入额外中间件。

---

### 删除文件

#### `mian.py`（已删除）
- 用户确认后删除了因拼写错误创建的 `mian.py` 文件。
- 全局搜索确认无残留引用。

---

### 技术决策记录

| 决策 | 选择 | 原因 |
|------|------|------|
| 异步执行 | 单机队列 + 后台线程 | 推理 pipeline 非线程安全，单 GPU 场景无需分布式队列 |
| 状态持久化 | 内存字典（v1） | 快速上线，服务重启后状态丢失可接受；后续可替换为数据库层 |
| 鉴权 | 无鉴权 | 内网/开发环境，接口契约不变的前提下后续可叠加认证中间件 |
| 模型加载 | 懒加载 + 单例 | API 启动快，VRAM 仅在实际任务时占用 |
| `reload=False` | 禁用 Uvicorn 热重载 | 热重载会 fork 进程/线程，与 Worker 单例模式冲突 |
