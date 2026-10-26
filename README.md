# Colab MiniMax H3 (ComfyUI) 按需自动生命周期网关 & API

本项目基于您本机已登录认证的 `colab` CLI，使用 **Docker Compose** 部署一个固定入口的 Web 控制与反向代理网关，实现 **MiniMax H3 (ComfyUI)** 的全自动生命周期管理与 API 调用。

## 核心工作流程

```mermaid
flowchart TD
    A["打开固定网址并登录 / 调用 API"] --> B["控制服务检查 Colab 实例"]
    B -->|已就绪| D["进入 ComfyUI / 执行生成任务"]
    B -->|网页访问且未就绪| H["控制台显示实例状态，不申请 GPU"]
    H -->|手动点击启动实例| C["创建 Colab GPU 实例 (默认 L4) 并自动部署"]
    B -->|API 调用且未就绪| C
    C --> E["页面显示启动进度 (挂载云盘/装ComfyUI/链模型/建隧道)"]
    E -->|SSH反代与健康检查通过| D
    D --> F["无运行任务、无排队任务且闲置达到设定时间 (默认 30 分钟)"]
    F --> G["自动归档保存结果至本机与 Google Drive 并释放 GPU 实例"]
```

---

## 快速启动

```bash
cd /root/colab-minimax
docker compose up -d --build
```

- **固定 Web 访问地址**：`http://<服务器IP>:8188`
- **管理控制台面板**：`http://<服务器IP>:8188/_control/dashboard`
- **交互式 API 文档 (Swagger)**：`http://<服务器IP>:8188/docs`
- **默认登录账号 / 密码**：`admin` / `admin123`（可在 `.env` 中修改）
- **默认 API Key**：`sk-minimax-h3-colab-key`（可在 `.env` 中修改）

---

## API 接口调用说明

外部项目只需直接请求生成接口。**如果当前没有运行中的 Colab 实例，服务会自动申请 GPU、部署 ComfyUI + MiniMax H3、建立隧道、执行生成并保存结果**，调用方无需关心底层生命周期。

### 1. 生成视频 (`POST /api/v1/video/generate`)

#### 同步调用示例（等待生成完成直接返回视频地址）：
```bash
curl -X POST http://127.0.0.1:8188/api/v1/video/generate \
  -H "X-API-Key: sk-minimax-h3-colab-key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Realistic cinematic sci-fi dusk metropolis, flying cars between towers, rain reflections, stereo synth music and thunder audio.",
    "width": 1344,
    "height": 768,
    "duration": 5.0,
    "turbo_mode": true,
    "wait": true
  }'
```

#### 异步调用示例（立即返回 `task_id`，适合后台轮询）：
```bash
curl -X POST http://127.0.0.1:8188/api/v1/video/generate \
  -H "X-API-Key: sk-minimax-h3-colab-key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "A cute golden retriever puppy running through blooming flowers, joyful bark and gentle acoustic guitar music.",
    "width": 864,
    "height": 480,
    "duration": 5.0,
    "turbo_mode": true,
    "wait": false
  }'
```

### 2. 查询任务状态 (`GET /api/v1/video/tasks/{task_id}`)

```bash
curl http://127.0.0.1:8188/api/v1/video/tasks/task_xxxxxxxxxxxx \
  -H "X-API-Key: sk-minimax-h3-colab-key"
```

### 3. 下载已归档的视频结果 (`GET /api/v1/outputs/{file_path}`)

即使 Colab GPU 实例因闲置被自动释放，所有生成的视频均已持久化保存在本机 `./data/outputs/` 目录及 Google Drive，可随时通过接口或面板访问：

```bash
curl -O http://127.0.0.1:8188/api/v1/outputs/video/MiniMax_H3_00001.mp4 \
  -H "X-API-Key: sk-minimax-h3-colab-key"
```

### 4. 原生 ComfyUI 接口透明兼容

您也可以直接使用任何标准 ComfyUI 客户端连接 `http://<服务器IP>:8188`（在 Header 中带上 `X-API-Key: sk-minimax-h3-colab-key` 或 `?api_key=...`），直接调用 `/prompt`、`/upload/image`、`/history`、`/queue` 等原生端点。当实例处于休眠状态时，请求 `/prompt` 也会自动唤醒实例并转发执行。

---

## MCP Server (Model Context Protocol) 接入说明

本项目同时提供 **Stdio 本地命令模式** 和 **HTTP / SSE 远程模式** 两种 MCP 接入方式，方便您在 Claude Code、Claude Desktop、Cursor、Antigravity、Cherry Studio 等任意支持 MCP 的大模型客户端中直接通过自然语言调用 MiniMax H3 生成音视频或管理云端 GPU。

### 方式一：Stdio 模式（推荐本机/同服务器 LLM 客户端使用）

在您的 MCP 客户端配置文件中添加：

```json
{
  "mcpServers": {
    "minimax-h3-video": {
      "command": "python3",
      "args": ["/root/colab-minimax/mcp_server.py"],
      "env": {
        "MINIMAX_GATEWAY_URL": "http://127.0.0.1:8188",
        "MINIMAX_API_KEY": "sk-minimax-h3-colab-key"
      }
    }
  }
}
```

### 方式二：Streamable HTTP / SSE 远程模式（适合跨机器或 Web Agent 接入）

- **Streamable HTTP 端点 (`POST`)**：
  `http://<服务器IP>:8188/mcp?api_key=sk-minimax-h3-colab-key`
  （或通过 Header 传递 `X-API-Key: sk-minimax-h3-colab-key` / `Authorization: Bearer sk-minimax-h3-colab-key`）
- **SSE 端点 (`GET`)**：
  `http://<服务器IP>:8188/mcp/sse?api_key=sk-minimax-h3-colab-key`

### 提供的 MCP Tools 列表

| 工具名称 (`tool`) | 功能说明 |
| --- | --- |
| `generate_video` | 使用 MiniMax H3 生成音视频同步的视频（支持 T2V 文生视频、I2V 图生视频、FL2V 首尾帧控制，支持直接传入本地图片路径/URL/Base64；若实例休眠会自动唤醒并部署） |
| `get_video_task_status` | 根据 `task_id` 查询生成进度、完成状态及本地持久化文件路径/下载 URL |
| `list_video_tasks` | 查看最近的视频生成任务列表 |
| `list_generated_outputs` | 列出本地 `data/outputs` 中已持久化保存的所有视频文件（实例关闭后依然可用） |
| `get_instance_status` | 查看当前 Colab GPU 实例状态、GPU 型号、队列任务数、闲置倒计时与算力余额 |
| `wake_up_instance` | 主动唤醒并部署 Colab GPU (L4 / A100 / T4) 实例 |
| `stop_instance` | 同步保存所有生成结果至本地与 Google Drive，并立即释放 Colab GPU 实例 |
| `sync_outputs` | 手动将远端 Colab 实例中的输出同步到本地与 Google Drive |

