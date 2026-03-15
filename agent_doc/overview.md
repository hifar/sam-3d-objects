# SAM 3D Objects — API Service Overview

## 项目目标

为 SAM 3D Objects 推理流程提供一套基于 **FastAPI + Uvicorn** 的 RESTful API 服务，支持：

1. 上传图片，异步提交 3D 生成任务（Gaussian Splat → `.ply`）
2. 轮询任务状态（queued / running / succeeded / failed / canceled）
3. 任务完成后下载 `.ply`（可选 `.glb`）

---

## 目录结构

```
sam-3d-objects/
├── app_config.json        # 应用配置文件（含 TestMode）
├── main.py               # Uvicorn 启动入口，组合 API app
└── api/
    ├── __init__.py       # FastAPI app 工厂 + lifespan 生命周期钩子
  ├── app_config.py     # 读取 app_config.json 配置
    ├── models.py         # 数据模型：JobRecord dataclass + Pydantic 响应 Schema
    ├── store.py          # 线程安全的内存任务状态仓库 InMemoryJobStore
    ├── worker.py         # 后台单线程 Worker + 推理模型懒加载
    └── routes.py         # 全部 REST 路由
```

---

## REST API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/jobs` | 上传图片创建任务，立即返回 202 + `job_id` |
| `GET` | `/v1/jobs` | 分页列出全部任务（可按 status 过滤） |
| `GET` | `/v1/jobs/{job_id}` | 查询任务状态与进度 |
| `DELETE` | `/v1/jobs/{job_id}` | 取消排队中的任务 |
| `GET` | `/v1/jobs/{job_id}/result` | 获取产物清单（含下载 URL 和文件大小） |
| `GET` | `/v1/jobs/{job_id}/artifacts/ply` | 下载 `splat.ply` |
| `GET` | `/v1/jobs/{job_id}/artifacts/mesh_glb` | 下载 `mesh.glb`（需提交时 `generate_mesh=true`） |
| `GET` | `/v1/health` | 健康检查 |

---

## 任务状态机

```
         submit()
QUEUED ──────────► RUNNING ──► SUCCEEDED
   │                             
   └──── cancel() ──► CANCELED
                         │
                    pipeline error
                         ▼
                       FAILED
```

---

## 并发模型

- **单 Worker 线程**：推理 pipeline 非线程安全，且需要约 32 GB VRAM，严格串行执行。
- **请求线程与 Worker 完全隔离**：API 线程只做任务入队，Worker 线程独立消费。
- **推理模型懒加载**：API 进程立即启动，首个任务开始时才加载模型到 GPU。

---

## 存储约定

每个任务在 `storage/jobs/{job_id}/` 下独立存放：

```
storage/jobs/{job_id}/
├── inputs/
│   ├── image.png
│   └── mask.png     (可选)
└── outputs/
    ├── splat.ply    (必须产物)
    └── mesh.glb     (可选，generate_mesh=true 时生成)
```

存储根目录可通过环境变量 `SAM3D_STORAGE_ROOT` 覆盖（默认 `./storage`）。

---

## 配置

### 1) 应用配置文件（app_config.json）

```json
{
  "TestMode": false,
  "MockDataDir": "Test",
  "MockPlyFile": "mockup.ply",
  "MockGlbFile": "mockup.glb",
  "MockSleepSeconds": 10
}
```

字段说明：

- `TestMode`：为 `true` 时，Worker 不做真实推理。
- `MockDataDir`：mock 文件目录，默认是项目根目录下的 `Test/`。
- `MockPlyFile`：mock PLY 文件名。
- `MockGlbFile`：mock GLB 文件名。
- `MockSleepSeconds`：模拟耗时秒数，默认 10 秒。

TestMode 行为：

1. 任务进入 `running` 后，Worker 先 `sleep 10` 秒（可配置）。
2. 从 `Test/mockup.ply` 和 `Test/mockup.glb` 复制文件到任务输出目录。
3. 输出文件名固定为 `outputs/splat.ply` 与 `outputs/mesh.glb`。
4. 任务状态置为 `succeeded`，用于联调 Job/轮询/下载 API。

> 注意：如果 mock 文件不存在，任务会变为 `failed` 并返回错误信息。

### 2) 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `APP_CONFIG_FILE` | `app_config.json` | 应用配置文件路径 |
| `SAM3D_CONFIG_FILE` | `checkpoints/hf/pipeline.yaml` | 推理 pipeline 配置文件路径 |
| `SAM3D_STORAGE_ROOT` | `./storage` | 任务文件存储根目录 |

---

## 启动方式

```bash
# 设置 pipeline config（必须）
set SAM3D_CONFIG_FILE=checkpoints/hf/pipeline.yaml

# 启动服务（基于 uv）
uv run python main.py
```

Swagger UI：http://localhost:8000/docs  
ReDoc：http://localhost:8000/redoc

---

## 典型调用流程

```bash
# 1. 提交任务（上传图片）
curl -X POST http://localhost:8000/v1/jobs \
  -F "image=@photo.png" \
  -F "seed=42"
# → { "job_id": "...", "status": "queued", ... }

# 2. 轮询状态直到 succeeded
curl http://localhost:8000/v1/jobs/<job_id>
# → { "status": "running", "progress_stage": "running_stage1", ... }

# 3. 获取产物清单
curl http://localhost:8000/v1/jobs/<job_id>/result
# → { "artifacts": { "ply": { "url": "...", "size": 12345678 } } }

# 4. 下载 PLY
curl -OJ http://localhost:8000/v1/jobs/<job_id>/artifacts/ply
```

---

## 当前版本限制（v1）

- 任务状态存储在内存中，服务重启后丢失。
- 运行中的任务无法强制取消（仅支持取消排队中的任务）。
- 暂不鉴权（适用于内网/开发环境）。
- 无自动任务清理（需手动删除 `storage/jobs/` 目录下的旧任务）。

---

## TestMode 快速验证

1. 准备 `Test/mockup.ply` 与 `Test/mockup.glb`。
2. 将 `app_config.json` 中 `TestMode` 改为 `true`。
3. 启动服务并提交任务。
4. 约 10 秒后任务应变为 `succeeded`，可直接下载 mock 产物。
