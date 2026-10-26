import asyncio
import base64
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.auth import require_auth
from app.colab_manager import manager
from app.config import config, save_runtime_settings
from app.task_service import VideoGenerateRequest, task_service

mcp_router = APIRouter(tags=["MCP Server"])

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
                "wait": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否等待实例完全启动并通过健康检查后再返回（默认 False 后台异步启动）。",
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
    """If img_val is a local file path that exists, convert it to a data:...;base64,... string."""
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


async def execute_mcp_tool_inprocess(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Executes an MCP tool directly inside the FastAPI gateway process."""
    try:
        if name == "generate_video":
            req = VideoGenerateRequest(
                prompt=arguments["prompt"],
                width=int(arguments.get("width", 864)),
                height=int(arguments.get("height", 480)),
                duration=float(arguments.get("duration", 5.0)),
                seed=arguments.get("seed"),
                turbo_mode=bool(arguments.get("turbo_mode", True)),
                steps=arguments.get("steps"),
                text_encoder=arguments.get("text_encoder", "heretic"),
                first_frame_image=_normalize_image_input(arguments.get("first_frame_image")),
                last_frame_image=_normalize_image_input(arguments.get("last_frame_image")),
                wait=bool(arguments.get("wait", True)),
            )
            rec = await task_service.submit_task(req)
            data = rec.model_dump()
            if data.get("output_files"):
                data["drive_file_paths"] = [
                    f"{config.drive_base_dir}/outputs/{rel}" for rel in data["output_files"]
                ]
            return {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
                "isError": rec.status == "failed",
            }

        elif name == "get_video_task_status":
            task_id = arguments["task_id"]
            rec = task_service.tasks.get(task_id)
            if not rec:
                return {
                    "content": [{"type": "text", "text": f"Task '{task_id}' not found"}],
                    "isError": True,
                }
            data = rec.model_dump()
            if data.get("output_files"):
                data["drive_file_paths"] = [
                    f"{config.drive_base_dir}/outputs/{rel}" for rel in data["output_files"]
                ]
            return {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        elif name == "list_video_tasks":
            limit = int(arguments.get("limit", 10))
            items = [t.model_dump() for t in reversed(list(task_service.tasks.values()))][:limit]
            return {
                "content": [{"type": "text", "text": json.dumps({"tasks": items}, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        elif name == "list_generated_outputs":
            limit = int(arguments.get("limit", 20))
            manifest_file = config.data_dir / "drive_outputs.json"
            files = []
            if manifest_file.is_file():
                try:
                    items = json.loads(manifest_file.read_text(encoding="utf-8"))
                    for item in sorted(items, key=lambda x: x.get("mtime", 0), reverse=True)[:limit]:
                        rel = item["rel"]
                        files.append(
                            {
                                "name": rel,
                                "drive_path": item.get("drive_path", f"{config.drive_base_dir}/outputs/{rel}"),
                                "size_bytes": int(item.get("size", 0)),
                                "mtime": int(item.get("mtime", 0)),
                            }
                        )
                except Exception:
                    pass
            return {
                "content": [{"type": "text", "text": json.dumps({"files": files}, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        elif name == "get_instance_status":
            status = manager.get_status_dict()
            return {
                "content": [{"type": "text", "text": json.dumps(status, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        elif name == "wake_up_instance":
            gpu = arguments.get("gpu")
            wait = bool(arguments.get("wait", False))
            if wait:
                await manager.ensure_started(requested_gpu=gpu)
                await manager.wait_until_ready(timeout=1500)
            else:
                await manager.ensure_started(requested_gpu=gpu)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(manager.get_status_dict(), ensure_ascii=False, indent=2),
                    }
                ],
                "isError": False,
            }

        elif name == "stop_instance":
            await manager.stop_instance(reason="MCP 工具主动释放")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"ok": True, "phase": manager.phase.value, "message": "已保存输出并释放 Colab GPU 实例"},
                            ensure_ascii=False,
                            indent=2,
                        ),
                    }
                ],
                "isError": False,
            }

        elif name == "sync_outputs":
            synced = await manager.sync_outputs_from_remote()
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"ok": True, "synced_files": synced}, ensure_ascii=False, indent=2),
                    }
                ],
                "isError": False,
            }

        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"Tool execution error: {e}"}],
            "isError": True,
        }


async def handle_jsonrpc_message(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Handles a single MCP JSON-RPC 2.0 request/notification."""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications have no id and expect no response
    if msg_id is None:
        return None

    if method == "initialize":
        client_proto = params.get("protocolVersion", "2024-11-05")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": client_proto,
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": MCP_SERVER_INFO,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": MCP_TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        res = await execute_mcp_tool_inprocess(tool_name, tool_args)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": res,
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


# Active SSE sessions for MCP SSE transport
_sse_queues: dict[str, asyncio.Queue] = {}


@mcp_router.post("/mcp", dependencies=[Depends(require_auth)])
async def mcp_streamable_http(request: Request):
    """MCP Streamable HTTP endpoint (JSON-RPC 2.0 over HTTP POST)."""
    body = await request.json()
    if isinstance(body, list):
        responses = []
        for item in body:
            r = await handle_jsonrpc_message(item)
            if r is not None:
                responses.append(r)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(content=responses)
    else:
        resp = await handle_jsonrpc_message(body)
        if resp is None:
            return Response(status_code=202)
        return JSONResponse(content=resp)


@mcp_router.get("/mcp/sse", dependencies=[Depends(require_auth)])
async def mcp_sse_connect(request: Request):
    """MCP SSE endpoint for SSE-based MCP clients."""
    session_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _sse_queues[session_id] = queue

    api_key_param = request.query_params.get("api_key", "")
    post_endpoint = f"/mcp/messages?session_id={session_id}"
    if api_key_param:
        post_endpoint += f"&api_key={api_key_param}"

    async def event_generator():
        try:
            yield f"event: endpoint\ndata: {post_endpoint}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: message\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sse_queues.pop(session_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@mcp_router.post("/mcp/messages", dependencies=[Depends(require_auth)])
async def mcp_sse_messages(request: Request, session_id: str):
    """MCP SSE message receiver endpoint."""
    queue = _sse_queues.get(session_id)
    if not queue:
        return JSONResponse(status_code=404, content={"error": "SSE session not found"})

    body = await request.json()
    resp = await handle_jsonrpc_message(body)
    if resp is not None:
        await queue.put(resp)
    return Response(status_code=202, content="Accepted")
