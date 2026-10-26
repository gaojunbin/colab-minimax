import asyncio
import logging
from typing import Optional

import httpx
import websockets
from fastapi import Request, Response, WebSocket, WebSocketDisconnect
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from app.colab_manager import manager, InstancePhase
from app.config import config

logger = logging.getLogger("proxy")

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

FLOATING_WIDGET_INJECTION = """
<!-- Colab MiniMax H3 Lifecycle Floating Widget -->
<style>
  #cmh3-floating-bar {
    position: fixed;
    bottom: 14px;
    left: 14px;
    z-index: 999999;
    background: rgba(15, 23, 42, 0.92);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(99, 102, 241, 0.45);
    border-radius: 999px;
    padding: 6px 14px;
    color: #e2e8f0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 10px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
    user-select: none;
  }
  #cmh3-floating-bar .cmh3-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #10b981;
    box-shadow: 0 0 8px #10b981;
  }
  #cmh3-floating-bar a, #cmh3-floating-bar button {
    color: #a5b4fc;
    background: rgba(99, 102, 241, 0.15);
    border: 1px solid rgba(99, 102, 241, 0.3);
    border-radius: 999px;
    padding: 2px 9px;
    font-size: 11px;
    text-decoration: none;
    cursor: pointer;
    transition: all 0.15s;
  }
  #cmh3-floating-bar a:hover, #cmh3-floating-bar button:hover {
    background: rgba(99, 102, 241, 0.35);
    color: #fff;
  }
</style>
<div id="cmh3-floating-bar">
  <span class="cmh3-dot" id="cmh3-dot"></span>
  <span id="cmh3-info">Colab GPU 运行中</span>
  <a href="/_control/dashboard" target="_blank">控制面板 & API</a>
  <button id="cmh3-sync-btn" onclick="window.__cmh3Sync()">同步结果</button>
  <button id="cmh3-stop-btn" style="color:#fca5a5;border-color:rgba(239,68,68,0.35);background:rgba(239,68,68,0.15)" onclick="window.__cmh3Stop()">释放实例</button>
</div>
<script>
(function(){
  async function pollStatus() {
    try {
      const r = await fetch('/api/v1/instance/status?passive=1');
      if (!r.ok) return;
      const d = await r.json();
      if (d.phase !== 'READY') {
        window.location.href = '/_control/dashboard';
        return;
      }
      const mins = Math.floor(d.remaining_idle_seconds / 60);
      const secs = d.remaining_idle_seconds % 60;
      const busy = (d.running_jobs > 0 || d.queued_jobs > 0) ? `任务运行中(${d.running_jobs}/${d.queued_jobs})` : `闲置倒计时 ${mins}m${secs}s`;
      document.getElementById('cmh3-info').textContent = `Colab ${d.active_gpu} · ${busy}`;
    } catch(e) {}
  }
  window.__cmh3Sync = async function() {
    const btn = document.getElementById('cmh3-sync-btn');
    btn.textContent = '同步中...';
    try {
      await fetch('/api/v1/instance/sync', {method: 'POST'});
      btn.textContent = '已同步!';
      setTimeout(() => btn.textContent = '同步结果', 2000);
    } catch(e) { btn.textContent = '同步结果'; }
  };
  window.__cmh3Stop = async function() {
    if (!confirm('确定要保存当前生成结果并立即释放 Colab GPU 实例吗？')) return;
    await fetch('/api/v1/instance/stop', {method: 'POST'});
    window.location.href = '/_control/dashboard';
  };
  setInterval(pollStatus, 8000);
  pollStatus();
})();
</script>
""".encode("utf-8")


async def proxy_http_request(request: Request, path: str) -> Response:
    """Forwards HTTP requests to local SSH-tunneled ComfyUI port."""
    manager.touch_activity(f"http:{path}")

    # Preserve raw percent-encoding in URL path (e.g. %2F in /api/userdata/workflows%2F...)
    raw_path = request.scope.get("raw_path", b"").decode("latin-1").lstrip("/")
    actual_path = raw_path if raw_path else path

    target_url = f"http://127.0.0.1:{config.local_comfy_port}/{actual_path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() != "accept-encoding"
    }
    body = await request.body()

    # Check if root HTML page so we can inject the floating control bar
    is_root_html = path in ("", "index.html") and request.method == "GET"

    if is_root_html:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
            content = resp.content
            if b"</body>" in content:
                content = content.replace(b"</body>", FLOATING_WIDGET_INJECTION + b"</body>")
            resp_headers = {
                k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
            }
            return Response(content=content, status_code=resp.status_code, headers=resp_headers)

    client = httpx.AsyncClient(timeout=None)
    req = client.build_request(
        method=request.method,
        url=target_url,
        headers=headers,
        content=body,
    )
    resp = await client.send(req, stream=True)
    resp_headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
    }
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=resp_headers,
        background=BackgroundTask(client.aclose),
    )


async def proxy_websocket(ws: WebSocket, path: str) -> None:
    """Bridges Browser/Client WebSocket <-> Tunneled ComfyUI WebSocket."""
    await ws.accept()
    manager.touch_activity("ws_connect")

    query = ws.url.query
    target_ws_url = f"ws://127.0.0.1:{config.local_comfy_port}/{path}"
    if query:
        target_ws_url += f"?{query}"

    try:
        async with websockets.connect(
            target_ws_url,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        ) as upstream:

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        manager.touch_activity("ws_client_msg")
                        if msg.get("text") is not None:
                            await upstream.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    pass
                except Exception:
                    pass

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream:
                        if isinstance(message, str):
                            await ws.send_text(message)
                        else:
                            await ws.send_bytes(message)
                except Exception:
                    pass

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except Exception as e:
        logger.debug(f"WebSocket proxy closed: {e}")
    finally:
        try:
            await ws.close()
        except Exception:
            pass
