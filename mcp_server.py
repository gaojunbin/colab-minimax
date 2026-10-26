#!/usr/bin/env python3
"""
Standalone Model Context Protocol (MCP) Stdio Server for Colab MiniMax H3 (ComfyUI).

Can be registered directly in any MCP-compatible LLM client (Claude Code, Claude Desktop,
Cursor, Antigravity, Cline, Cherry Studio, etc.):

{
  "mcpServers": {
    "minimax-h3-video": {
      "command": "python3",
      "args": ["/root/colab-minimax/mcp_server.py"]
    }
  }
}
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

PROJECT_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> dict[str, str]:
    env_map: dict[str, str] = {}
    env_file = PROJECT_DIR / ".env"
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env_map[k.strip()] = v.strip().strip("'").strip('"')
    return env_map


_DOTENV = _load_dotenv()
PORT = os.getenv("PORT", _DOTENV.get("PORT", "8188"))
GATEWAY_URL = os.getenv("MINIMAX_GATEWAY_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
API_KEY = os.getenv("MINIMAX_API_KEY", os.getenv("API_KEY", _DOTENV.get("API_KEY", "sk-minimax-h3-colab-key")))

MCP_SERVER_INFO = {
    "name": "colab-minimax-h3-mcp",
    "version": "1.1.0",
}

MCP_TOOLS = [
    {
        "name": "generate_video",
        "description": (
            "使用云端 Google Colab GPU 上的 MiniMax H3 (ComfyUI) 生成音视频同步的视频（支持文生视频 T2V、图生视频 I2V、首尾帧控制 FL2V）。"
            "如果当前 Colab GPU 实例未运行或已被回收，会自动创建 GPU、挂载 Google Drive 模型并完成生成，最后保存至本地持久化目录。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "详细的视频画面、主体动作、运镜方式以及伴随音频（人声台词、环境音效、背景音乐）提示词。",
                },
                "width": {
                    "type": "integer",
                    "default": 864,
                    "description": "视频宽度（自动对齐 32 倍数，推荐 864 或 1344）。",
                },
                "height": {
                    "type": "integer",
                    "default": 480,
                    "description": "视频高度（自动对齐 32 倍数，推荐 480 或 768）。",
                },
                "duration": {
                    "type": "number",
                    "default": 5.0,
                    "description": "视频时长（秒，支持 2.0 ~ 15.0 秒，帧率 24fps）。",
                },
                "turbo_mode": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否启用 Turbo LoRA 极速模式（默认 True，仅需 8 步或 4 步）。",
                },
                "steps": {
                    "type": "integer",
                    "description": "采样步数（Turbo 模式可设 8 或 4，标准模式默认 20）。",
                },
                "text_encoder": {
                    "type": "string",
                    "enum": ["heretic", "official"],
                    "default": "heretic",
                    "description": "文本编码器选择：'heretic' (Momoking/Qwen3-VL-32B-Heretic-MiniMax-H3-NVFP4) 或 'official' (官方 AWQ)。",
                },
                "first_frame_image": {
                    "type": "string",
                    "description": "可选：首帧参考图（支持本地文件绝对路径、HTTP/HTTPS URL 或 Base64 字符串），用于图生视频 (I2V) 或首尾帧生成 (FL2V)。",
                },
                "last_frame_image": {
                    "type": "string",
                    "description": "可选：尾帧参考图（支持本地文件绝对路径、HTTP/HTTPS URL 或 Base64 字符串），用于首尾帧视频生成 (FL2V)。",
                },
                "seed": {
                    "type": "integer",
                    "description": "可选随机种子，不填则随机生成。",
                },
                "wait": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否等待视频生成完成后再返回结果（默认 True；若设为 False 则立即返回 task_id 供后续轮询）。",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "get_video_task_status",
        "description": "根据 task_id 查询视频生成任务的实时状态、进度信息以及生成完成后的本地视频路径和访问 URL。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "generate_video 返回的任务 ID（例如 task_xxxxxxxxxxxx）。",
                }
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "list_video_tasks",
        "description": "列出最近的 MiniMax H3 视频生成任务记录及其状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "返回的最大任务条数（默认 10）。",
                }
            },
        },
    },
    {
        "name": "list_generated_outputs",
        "description": "列出所有已持久化保存在本地磁盘 (data/outputs) 的视频/图像文件列表（即使 Colab 实例已关闭也可随时访问）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "返回的最大文件数量（默认 20）。",
                }
            },
        },
    },
    {
        "name": "get_instance_status",
        "description": "查询当前 Google Colab GPU 实例生命周期状态（是否就绪、当前 GPU 型号、运行/排队任务数、闲置倒计时、算力余额及最新日志）。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "wake_up_instance",
        "description": "主动唤醒或启动 Google Colab GPU 实例并部署 ComfyUI (MiniMax H3) 环境。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "gpu": {
                    "type": "string",
                    "enum": ["L4", "A100", "T4"],
                    "description": "可选请求的 GPU 型号（默认使用系统配置的 L4）。",
                },
            },
        },
    },
    {
        "name": "stop_instance",
        "description": "将远端 Colab 实例中的生成结果同步保存到本地与 Google Drive，然后立即释放 Colab GPU 实例以停止计费。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "sync_outputs",
        "description": "手动触发将远端 Colab 实例上的生成结果同步到本地持久化目录与 Google Drive。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def _normalize_image_input(img_val: Optional[str]) -> Optional[str]:
    if not img_val:
        return None
    val = img_val.strip()
    if val.startswith(("http://", "https://", "data:")):
        return val
    p = Path(val).expanduser()
    if p.is_file():
        ext = p.suffix.lower().lstrip(".") or "png"
        if ext == "jpg":
            ext = "jpeg"
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"data:image/{ext};base64,{b64}"
    return val


def _api_request(method: str, path: str, payload: Optional[dict] = None, timeout: float = 1800.0) -> dict[str, Any]:
    url = f"{GATEWAY_URL}{path}"
    data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "X-API-Key": API_KEY,
        "Accept": "application/json",
    }
    if data_bytes is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {path}: {err_body}") from e
    except Exception as e:
        raise RuntimeError(f"Request to {url} failed: {e}") from e


def _enrich_task_record(data: dict[str, Any]) -> dict[str, Any]:
    drive_base = _DOTENV.get("DRIVE_BASE_DIR", "/content/drive/MyDrive/Colab-MiniMax-H3")
    if data.get("output_files"):
        data["drive_file_paths"] = [
            f"{drive_base}/outputs/{rel}" for rel in data["output_files"]
        ]
    return data


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "generate_video":
            payload = {
                "prompt": arguments["prompt"],
                "width": int(arguments.get("width", 864)),
                "height": int(arguments.get("height", 480)),
                "duration": float(arguments.get("duration", 5.0)),
                "turbo_mode": bool(arguments.get("turbo_mode", True)),
                "text_encoder": arguments.get("text_encoder", "heretic"),
                "wait": bool(arguments.get("wait", True)),
            }
            if arguments.get("steps") is not None:
                payload["steps"] = int(arguments["steps"])
            if arguments.get("seed") is not None:
                payload["seed"] = int(arguments["seed"])
            if arguments.get("first_frame_image"):
                payload["first_frame_image"] = _normalize_image_input(arguments["first_frame_image"])
            if arguments.get("last_frame_image"):
                payload["last_frame_image"] = _normalize_image_input(arguments["last_frame_image"])

            res = _api_request("POST", "/api/v1/video/generate", payload=payload, timeout=1800.0)
            res = _enrich_task_record(res)
            return {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}],
                "isError": res.get("status") == "failed",
            }

        elif name == "get_video_task_status":
            task_id = urllib.parse.quote(str(arguments["task_id"]))
            res = _api_request("GET", f"/api/v1/video/tasks/{task_id}", timeout=30.0)
            res = _enrich_task_record(res)
            return {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        elif name == "list_video_tasks":
            limit = int(arguments.get("limit", 10))
            res = _api_request("GET", "/api/v1/video/tasks", timeout=30.0)
            tasks = [_enrich_task_record(t) for t in res.get("tasks", [])[:limit]]
            return {
                "content": [{"type": "text", "text": json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        elif name == "list_generated_outputs":
            limit = int(arguments.get("limit", 20))
            res = _api_request("GET", "/api/v1/outputs", timeout=30.0)
            files = res.get("files", [])[:limit]
            return {
                "content": [{"type": "text", "text": json.dumps({"files": files}, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        elif name == "get_instance_status":
            res = _api_request("GET", "/api/v1/instance/status?passive=1", timeout=30.0)
            return {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        elif name == "wake_up_instance":
            gpu = arguments.get("gpu")
            q = f"?gpu={urllib.parse.quote(gpu)}" if gpu else ""
            res = _api_request("POST", f"/api/v1/instance/start{q}", payload={}, timeout=60.0)
            return {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        elif name == "stop_instance":
            res = _api_request("POST", "/api/v1/instance/stop", payload={}, timeout=180.0)
            return {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        elif name == "sync_outputs":
            res = _api_request("POST", "/api/v1/instance/sync", payload={}, timeout=180.0)
            return {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"MCP Tool Error ({name}): {e}"}],
            "isError": True,
        }


def handle_rpc(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if msg_id is None:
        return None

    if method == "initialize":
        proto = params.get("protocolVersion", "2024-11-05")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": MCP_SERVER_INFO,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}

    if method == "tools/call":
        t_name = params.get("name", "")
        t_args = params.get("arguments") or {}
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": call_tool(t_name, t_args),
        }

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue

        if isinstance(msg, list):
            out = [r for item in msg if (r := handle_rpc(item)) is not None]
            if out:
                sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        elif isinstance(msg, dict):
            resp = handle_rpc(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    main()
