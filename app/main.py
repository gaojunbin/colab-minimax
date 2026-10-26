import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import (
    SESSION_COOKIE_NAME,
    create_session_token,
    is_authenticated,
    require_auth,
)
from app.colab_manager import InstancePhase, manager
from app.config import config, load_runtime_settings, save_runtime_settings
from app.mcp_server import mcp_router
from app.proxy import proxy_http_request, proxy_websocket
from app.task_service import VideoGenerateRequest, task_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

templates = Jinja2Templates(directory="/app/app/templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_runtime_settings()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    await manager.start_background_monitor()
    yield


app = FastAPI(
    title="Colab MiniMax H3 (ComfyUI) Lifecycle Gateway, REST API & MCP Server",
    description=(
        "Automated on-demand Google Colab GPU provisioning, ComfyUI MiniMax H3 deployment, "
        "reverse proxy, idle auto-shutdown, video generation REST API, and Model Context Protocol (MCP) Server."
    ),
    version="1.1.0",
    lifespan=lifespan,
)

app.include_router(mcp_router)


# ============================================================================
# 1. Authentication & Control Panel Routes (/_control/*)
# ============================================================================


@app.get("/_control/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str = "/"):
    if is_authenticated(request):
        return RedirectResponse(url=next or "/", status_code=302)
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "next_url": next}
    )


@app.post("/_control/login", response_class=HTMLResponse, include_in_schema=False)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    user_ok = hmac.compare_digest(username.strip(), config.web_username)
    pass_ok = hmac.compare_digest(password, config.web_password)
    if not (user_ok and pass_ok):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "用户名或密码错误，请重试", "next_url": next},
            status_code=401,
        )

    token = create_session_token(config.web_username)
    resp = RedirectResponse(url=next or "/", status_code=302)
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=86400 * 30,
        samesite="lax",
    )
    return resp


@app.get("/_control/logout", include_in_schema=False)
async def logout():
    resp = RedirectResponse(url="/_control/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@app.get("/_control/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page(request: Request, auto: int = 0):
    if not is_authenticated(request):
        return RedirectResponse(url="/_control/login?next=/_control/dashboard", status_code=302)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "auto_redirect": bool(auto),
            "api_key": config.api_key,
            "port": config.port,
            "drive_base_dir": config.drive_base_dir,
        },
    )


@app.get("/_control/integration", response_class=HTMLResponse, include_in_schema=False)
async def integration_docs_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/_control/login?next=/_control/integration", status_code=302)
    return templates.TemplateResponse(
        request,
        "integration_docs.html",
        {
            "api_key": config.api_key,
            "port": config.port,
            "drive_base_dir": config.drive_base_dir,
        },
    )


# ============================================================================
# 2. Instance Lifecycle & Video Generation REST API (/api/v1/*)
# ============================================================================


@app.get("/api/v1/health", tags=["System"])
async def health_check():
    """Public health check endpoint for container orchestration."""
    return {
        "ok": True,
        "phase": manager.phase.value,
        "active_gpu": manager.active_gpu or config.default_gpu,
    }


@app.get("/api/v1/instance/status", tags=["Instance Lifecycle"], dependencies=[Depends(require_auth)])
async def api_instance_status(passive: int = 0):
    """Returns detailed Colab GPU instance status, progress, queue counts, idle timer, and recent logs."""
    if not passive:
        manager.touch_activity("api_status")
    return manager.get_status_dict()


@app.post("/api/v1/instance/start", tags=["Instance Lifecycle"], dependencies=[Depends(require_auth)])
async def api_instance_start(gpu: Optional[str] = None):
    """Triggers provisioning and deployment of the Colab GPU ComfyUI instance if not already running."""
    ready = await manager.ensure_started(requested_gpu=gpu)
    return {
        "ok": True,
        "ready": ready,
        "phase": manager.phase.value,
        "status": manager.get_status_dict(),
    }


@app.post("/api/v1/instance/stop", tags=["Instance Lifecycle"], dependencies=[Depends(require_auth)])
async def api_instance_stop():
    """Saves generated outputs from Colab to persistent storage and releases the Colab GPU instance."""
    await manager.stop_instance(reason="API/控制台主动释放")
    return {"ok": True, "phase": manager.phase.value}


@app.post("/api/v1/instance/sync", tags=["Instance Lifecycle"], dependencies=[Depends(require_auth)])
async def api_instance_sync():
    """Manually syncs generated outputs from Colab VM to local ./data/outputs and Google Drive."""
    files = await manager.sync_outputs_from_remote()
    return {"ok": True, "synced_files": files}


@app.post("/api/v1/instance/drive-complete", tags=["Instance Lifecycle"], dependencies=[Depends(require_auth)])
async def api_drive_complete():
    """Completes interactive Google Drive mount after user authorizes in browser."""
    ok, msg = await manager.complete_drive_mount()
    return {"ok": ok, "message": msg}


@app.post("/api/v1/instance/settings", tags=["Instance Lifecycle"], dependencies=[Depends(require_auth)])
async def api_update_settings(payload: dict):
    """Updates default GPU type, idle auto-shutdown timeout, and Drive persistence settings."""
    updated = save_runtime_settings(payload)
    return {"ok": True, "settings": updated}


@app.post("/api/v1/video/generate", tags=["MiniMax H3 Video Generation"], dependencies=[Depends(require_auth)])
async def api_generate_video(req: VideoGenerateRequest):
    """
    Submit a MiniMax H3 video generation request (Text-to-Video, Image-to-Video, or First-Last-Frame).
    If the Colab GPU instance is currently sleeping/reclaimed, this service automatically allocates
    a GPU, deploys ComfyUI + MiniMax H3, executes the workflow, saves the output locally, and returns
    the result.
    """
    rec = await task_service.submit_task(req)
    return rec.model_dump()


@app.get("/api/v1/video/tasks", tags=["MiniMax H3 Video Generation"], dependencies=[Depends(require_auth)])
async def api_list_tasks():
    """List recent video generation tasks."""
    items = [t.model_dump() for t in reversed(list(task_service.tasks.values()))]
    return {"tasks": items[:50]}


@app.get("/api/v1/video/tasks/{task_id}", tags=["MiniMax H3 Video Generation"], dependencies=[Depends(require_auth)])
async def api_get_task(task_id: str):
    """Query the status and output video URL of a specific video generation task."""
    rec = task_service.tasks.get(task_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return rec.model_dump()


@app.get("/api/v1/outputs", tags=["Google Drive Outputs"], dependencies=[Depends(require_auth)])
async def api_list_outputs():
    """Lists all generated video/audio/image outputs saved in Google Drive."""
    import json

    manifest_file = config.data_dir / "drive_outputs.json"
    files = []
    if manifest_file.is_file():
        try:
            items = json.loads(manifest_file.read_text(encoding="utf-8"))
            for item in sorted(items, key=lambda x: x.get("mtime", 0), reverse=True):
                rel = item["rel"]
                files.append(
                    {
                        "name": rel,
                        "drive_path": item.get("drive_path", f"{config.drive_base_dir}/outputs/{rel}"),
                        "size": int(item.get("size", 0)),
                        "mtime": int(item.get("mtime", 0)),
                        "url": f"/api/v1/outputs/{rel}",
                    }
                )
        except Exception:
            pass
    return {"files": files}


@app.get("/api/v1/outputs/{file_path:path}", tags=["Google Drive Outputs"], dependencies=[Depends(require_auth)])
async def api_download_output(file_path: str):
    """Streams an output file directly from the remote Colab ComfyUI instance (Google Drive mount) without saving to local VPS disk."""
    import httpx
    from fastapi.responses import Response

    if manager.phase != InstancePhase.READY:
        raise HTTPException(
            status_code=404,
            detail=(
                f"文件已保存在 Google Drive ({config.drive_base_dir}/outputs/{file_path})。"
                "当前本地 VPS 不存储视频副本，请直接在 Google Drive 云盘中查看，或唤醒实例后在线预览。"
            ),
        )

    p = Path(file_path)
    view_params = {
        "filename": p.name,
        "subfolder": "" if str(p.parent) == "." else str(p.parent),
        "type": "output",
    }
    comfy_base = f"http://127.0.0.1:{config.local_comfy_port}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(f"{comfy_base}/view", params=view_params)
        if resp.status_code != 200:
            raise HTTPException(status_code=404, detail="Output file not found on remote Google Drive")
        return Response(
            content=resp.content,
            media_type=resp.headers.get("content-type", "application/octet-stream"),
        )


# ============================================================================
# 3. Root Entrypoint & Transparent Reverse Proxy to ComfyUI
# ============================================================================


@app.websocket("/{path:path}")
async def ws_catch_all(ws: WebSocket, path: str):
    # Verify cookie or api_key on websocket
    cookie_token = ws.cookies.get(SESSION_COOKIE_NAME)
    q_key = ws.query_params.get("api_key")
    from app.auth import verify_session_token

    if not (verify_session_token(cookie_token) or (q_key and hmac.compare_digest(q_key, config.api_key))):
        await ws.close(code=1008)
        return

    if manager.phase != InstancePhase.READY:
        await ws.close(code=1013)
        return

    await proxy_websocket(ws, path)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def catch_all_proxy(request: Request, path: str):
    # Allow public static assets (.js, .css, .woff2, .png, .svg, .ico, .webp, .json in assets/locales) so ES module imports without credentials don't 401
    is_static_asset = path.startswith(("assets/", "locales/", "extensions/", "templates/")) or path.endswith(
        (".js", ".css", ".woff", ".woff2", ".ttf", ".ico", ".svg")
    )
    # 1. Authenticate user (redirect browser to login page, or 401 for API requests)
    if not is_static_asset and not is_authenticated(request):
        accept = request.headers.get("accept", "")
        if "text/html" in accept or path == "":
            return RedirectResponse(url=f"/_control/login?next=/{path}", status_code=302)
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    # 2. Check if ComfyUI instance is ready
    if manager.phase == InstancePhase.READY:
        healthy, r, p = await manager.check_local_comfy_health()
        if healthy:
            manager.running_jobs_count = r
            manager.queued_jobs_count = p
            return await proxy_http_request(request, path)

    # Static assets only exist while ComfyUI is running. They skip auth above, so they
    # must never auto-boot the GPU or receive the console page.
    if is_static_asset:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    # 3. Instance is not ready!
    # If this is a programmatic ComfyUI API call (e.g., POST /prompt or GET /history), auto-boot and wait!
    is_browser_nav = "text/html" in request.headers.get("accept", "") and request.method == "GET"
    if not is_browser_nav and path in ("prompt", "upload/image", "queue", "history", "system_stats", "object_info"):
        await manager.wait_until_ready(timeout=1200)
        return await proxy_http_request(request, path)

    # 4. Browser user visiting root or a page while the instance is not ready:
    # show the console without booting; the GPU starts only from its start button.
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "auto_redirect": True,
            "api_key": config.api_key,
            "port": config.port,
            "drive_base_dir": config.drive_base_dir,
        },
    )
