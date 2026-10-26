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
  @keyframes cmh3-in {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: none; }
  }
  @keyframes cmh3-pop {
    from { opacity: 0; transform: translateY(10px) scale(0.97); }
    to { opacity: 1; transform: none; }
  }
  #cmh3-floating-bar {
    position: fixed;
    bottom: 14px;
    left: 14px;
    z-index: 999999;
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 4px 4px 4px 12px;
    border-radius: 999px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    background: rgba(28, 28, 30, 0.78);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    backdrop-filter: saturate(180%) blur(20px);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.32);
    color: rgba(255, 255, 255, 0.92);
    font: 500 13px/1.2 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    user-select: none;
    animation: cmh3-in 420ms cubic-bezier(0.22, 1, 0.36, 1) both;
  }
  #cmh3-dot {
    width: 8px;
    height: 8px;
    flex-shrink: 0;
    border-radius: 50%;
    background: #34c759;
    box-shadow: 0 0 0 3px rgba(52, 199, 89, 0.22);
  }
  #cmh3-info {
    margin: 0 6px 0 8px;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  #cmh3-floating-bar .cmh3-sep {
    width: 1px;
    height: 14px;
    margin: 0 4px;
    background: rgba(255, 255, 255, 0.14);
  }
  #cmh3-floating-bar a,
  #cmh3-floating-bar button {
    all: unset;
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    height: 28px;
    padding: 0 11px;
    border-radius: 999px;
    color: rgba(255, 255, 255, 0.86);
    font: inherit;
    cursor: pointer;
    transition: background-color 150ms ease, color 150ms ease;
  }
  #cmh3-floating-bar a:hover,
  #cmh3-floating-bar button:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #fff;
  }
  #cmh3-floating-bar a:focus-visible,
  #cmh3-floating-bar button:focus-visible {
    outline: 2px solid #0a84ff;
    outline-offset: 1px;
  }
  #cmh3-floating-bar svg {
    display: block;
    width: 12px;
    height: 12px;
  }
  #cmh3-floating-bar #cmh3-stop-btn {
    color: #ff6961;
  }
  #cmh3-floating-bar #cmh3-stop-btn:hover {
    background: rgba(255, 69, 58, 0.16);
    color: #ff453a;
  }
  #cmh3-confirm {
    width: min(380px, calc(100vw - 32px));
    margin: auto;
    padding: 0;
    border: 0;
    border-radius: 20px;
    background: #fff;
    color: #1d1d1f;
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.35);
    font: 400 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  #cmh3-confirm[open] {
    animation: cmh3-pop 360ms cubic-bezier(0.32, 0.72, 0, 1);
  }
  #cmh3-confirm::backdrop {
    background: rgba(0, 0, 0, 0.45);
  }
  #cmh3-confirm form {
    margin: 0;
    padding: 26px 24px 22px;
  }
  #cmh3-confirm h2 {
    margin: 0;
    font-size: 19px;
    font-weight: 600;
    line-height: 1.25;
    letter-spacing: -0.02em;
    color: #1d1d1f;
  }
  #cmh3-confirm p {
    margin: 8px 0 0;
    font-size: 15px;
    color: #6e6e73;
  }
  #cmh3-confirm .cmh3-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 22px;
  }
  #cmh3-confirm button {
    all: unset;
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
    height: 38px;
    padding: 0 18px;
    border-radius: 999px;
    background: rgba(0, 0, 0, 0.05);
    color: #1d1d1f;
    font: 500 15px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    cursor: pointer;
    transition: background-color 150ms ease, transform 150ms ease;
  }
  #cmh3-confirm button:hover {
    background: rgba(0, 0, 0, 0.08);
  }
  #cmh3-confirm button:active {
    transform: scale(0.97);
  }
  #cmh3-confirm button:focus-visible {
    outline: 2px solid #0071e3;
    outline-offset: 2px;
  }
  #cmh3-confirm .cmh3-danger {
    background: #d70015;
    color: #fff;
  }
  #cmh3-confirm .cmh3-danger:hover {
    background: #b8000f;
  }
</style>
<div id="cmh3-floating-bar">
  <span id="cmh3-dot"></span>
  <span id="cmh3-info">GPU 运行中</span>
  <span class="cmh3-sep"></span>
  <a href="/_control/dashboard" target="_blank">控制台<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg></a>
  <button id="cmh3-sync-btn" type="button" onclick="window.__cmh3Sync()">同步</button>
  <button id="cmh3-stop-btn" type="button" onclick="window.__cmh3Stop()">释放</button>
</div>
<dialog id="cmh3-confirm">
  <form method="dialog">
    <h2>释放 GPU 实例？</h2>
    <p>生成结果会先同步到 Google Drive，随后释放实例并停止消耗算力。</p>
    <div class="cmh3-actions">
      <button value="cancel">取消</button>
      <button class="cmh3-danger" value="confirm">释放</button>
    </div>
  </form>
</dialog>
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
      const busy = d.running_jobs > 0 || d.queued_jobs > 0;
      const secs = d.remaining_idle_seconds;
      const idle = secs >= 60 ? `${Math.ceil(secs / 60)} 分钟后闲置释放` : '即将闲置释放';
      document.getElementById('cmh3-info').textContent = busy
        ? `${d.active_gpu} · 运行 ${d.running_jobs} · 排队 ${d.queued_jobs}`
        : `${d.active_gpu} · ${idle}`;
    } catch(e) {}
  }
  function confirmStop() {
    const dlg = document.getElementById('cmh3-confirm');
    if (!dlg || typeof dlg.showModal !== 'function') {
      return Promise.resolve(confirm('确定要保存当前生成结果并立即释放 Colab GPU 实例吗？'));
    }
    return new Promise(function(resolve) {
      dlg.returnValue = '';
      dlg.addEventListener('close', function() { resolve(dlg.returnValue === 'confirm'); }, { once: true });
      dlg.showModal();
    });
  }
  window.__cmh3Sync = async function() {
    const btn = document.getElementById('cmh3-sync-btn');
    btn.textContent = '同步中…';
    try {
      await fetch('/api/v1/instance/sync', {method: 'POST'});
      btn.textContent = '已同步';
      setTimeout(() => btn.textContent = '同步', 2000);
    } catch(e) { btn.textContent = '同步'; }
  };
  window.__cmh3Stop = async function() {
    if (!(await confirmStop())) return;
    const btn = document.getElementById('cmh3-stop-btn');
    btn.textContent = '释放中…';
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
