import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx

from app.config import config

logger = logging.getLogger("colab_manager")


class InstancePhase(str, Enum):
    STOPPED = "STOPPED"
    ALLOCATING_VM = "ALLOCATING_VM"
    MOUNTING_DRIVE = "MOUNTING_DRIVE"
    INSTALLING_COMFYUI = "INSTALLING_COMFYUI"
    DOWNLOADING_MODELS = "DOWNLOADING_MODELS"
    STARTING_COMFYUI = "STARTING_COMFYUI"
    ESTABLISHING_TUNNEL = "ESTABLISHING_TUNNEL"
    READY = "READY"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


PHASE_PROGRESS = {
    InstancePhase.STOPPED: 0,
    InstancePhase.ALLOCATING_VM: 15,
    InstancePhase.MOUNTING_DRIVE: 30,
    InstancePhase.INSTALLING_COMFYUI: 50,
    InstancePhase.DOWNLOADING_MODELS: 75,
    InstancePhase.STARTING_COMFYUI: 90,
    InstancePhase.ESTABLISHING_TUNNEL: 95,
    InstancePhase.READY: 100,
    InstancePhase.STOPPING: 10,
    InstancePhase.ERROR: 0,
}

PHASE_DESCRIPTIONS = {
    InstancePhase.STOPPED: "实例已休眠/未启动 (不产生算力费用)",
    InstancePhase.ALLOCATING_VM: "正在向 Google Colab 申请 GPU 实例...",
    InstancePhase.MOUNTING_DRIVE: "正在检查并挂载 Google Drive 持久化存储...",
    InstancePhase.INSTALLING_COMFYUI: "正在部署最新版 ComfyUI 及依赖组件...",
    InstancePhase.DOWNLOADING_MODELS: "正在同步/校验 MiniMax H3 模型权重 (支持云盘缓存秒级软链)...",
    InstancePhase.STARTING_COMFYUI: "正在启动 ComfyUI 引擎并加载节点...",
    InstancePhase.ESTABLISHING_TUNNEL: "正在建立 SSH 安全反代隧道并执行健康检查...",
    InstancePhase.READY: "ComfyUI (MiniMax H3) 已就绪，正在运行中",
    InstancePhase.STOPPING: "正在保存输出成果并释放 Colab GPU 实例...",
    InstancePhase.ERROR: "部署或连接过程中发生错误",
}


class ColabLifecycleManager:
    def __init__(self) -> None:
        self.phase: InstancePhase = InstancePhase.STOPPED
        self.phase_detail: str = ""
        self.error_message: Optional[str] = None
        self.drive_auth_url: Optional[str] = None

        self.active_gpu: Optional[str] = None
        self.endpoint: Optional[str] = None
        self.started_at: Optional[float] = None
        self.last_active_at: float = time.time()

        self.running_jobs_count: int = 0
        self.queued_jobs_count: int = 0
        self.active_api_tasks: int = 0

        self.compute_balance: Optional[float] = None
        self.compute_burn_rate: Optional[float] = None

        self.logs: deque[str] = deque(maxlen=400)
        self._boot_lock = asyncio.Lock()
        self._boot_task: Optional[asyncio.Task] = None
        self._tunnel_proc: Optional[asyncio.subprocess.Process] = None
        self._monitor_task: Optional[asyncio.Task] = None

        self.ssh_key_path = config.data_dir / "ssh" / "id_ed25519"
        self._ensure_ssh_key()

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.logs.append(line)
        logger.info(msg)

    def _ensure_ssh_key(self) -> None:
        self.ssh_key_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.ssh_key_path.exists():
            subprocess.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(self.ssh_key_path),
                ],
                check=False,
                capture_output=True,
            )
        # Also copy to ~/.ssh/id_ed25519 if missing so colab ssh finds it automatically
        root_ssh = Path(os.path.expanduser("~/.ssh"))
        root_ssh.mkdir(parents=True, exist_ok=True)
        root_key = root_ssh / "id_ed25519"
        root_pub = root_ssh / "id_ed25519.pub"
        if not root_key.exists() and self.ssh_key_path.exists():
            shutil.copy2(self.ssh_key_path, root_key)
            os.chmod(root_key, 0o600)
        pub_src = Path(f"{self.ssh_key_path}.pub")
        if not root_pub.exists() and pub_src.exists():
            shutil.copy2(pub_src, root_pub)

    def touch_activity(self, reason: str = "") -> None:
        self.last_active_at = time.time()

    @property
    def idle_seconds(self) -> int:
        if self.phase != InstancePhase.READY:
            return 0
        if self.running_jobs_count > 0 or self.queued_jobs_count > 0 or self.active_api_tasks > 0:
            self.last_active_at = time.time()
            return 0
        return max(0, int(time.time() - self.last_active_at))

    @property
    def remaining_idle_seconds(self) -> int:
        if self.phase != InstancePhase.READY:
            return config.idle_timeout_seconds
        return max(0, config.idle_seconds if hasattr(config, "idle_seconds") else config.idle_timeout_seconds - self.idle_seconds)

    def get_status_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "progress": PHASE_PROGRESS.get(self.phase, 0),
            "description": PHASE_DESCRIPTIONS.get(self.phase, ""),
            "detail": self.phase_detail,
            "error": self.error_message,
            "drive_auth_url": self.drive_auth_url,
            "session_name": config.colab_session_name,
            "active_gpu": self.active_gpu or config.default_gpu,
            "endpoint": self.endpoint,
            "started_at": self.started_at,
            "uptime_seconds": int(time.time() - self.started_at) if self.started_at and self.phase == InstancePhase.READY else 0,
            "idle_seconds": self.idle_seconds,
            "idle_timeout_seconds": config.idle_timeout_seconds,
            "remaining_idle_seconds": max(0, config.idle_timeout_seconds - self.idle_seconds),
            "running_jobs": self.running_jobs_count,
            "queued_jobs": self.queued_jobs_count,
            "active_api_tasks": self.active_api_tasks,
            "compute_balance": self.compute_balance,
            "compute_burn_rate": self.compute_burn_rate,
            "settings": {
                "default_gpu": config.default_gpu,
                "idle_timeout_seconds": config.idle_timeout_seconds,
                "enable_drive_mount": config.enable_drive_mount,
                "auto_start_on_visit": config.auto_start_on_visit,
            },
            "recent_logs": list(self.logs)[-80:],
        }

    async def start_background_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._lifecycle_loop())
            # Also check if there is already an active session on startup
            asyncio.create_task(self._initial_check())

    async def _initial_check(self) -> None:
        """On container startup, check if Colab session is already alive and reconnect if so."""
        try:
            await self.refresh_usage()
            sessions = await self._list_colab_sessions()
            if config.colab_session_name in sessions:
                info = sessions[config.colab_session_name]
                self.active_gpu = info.get("accelerator", config.default_gpu)
                self.endpoint = info.get("endpoint")
                self._log(f"检测到已有活跃 Colab 会话 [{config.colab_session_name}] ({self.active_gpu})，正在尝试恢复连接...")
                await self.ensure_started()
        except Exception as e:
            self._log(f"启动自检提示: {e}")

    async def refresh_usage(self) -> None:
        code, out, err = await self._run_colab_cmd(["usage"], timeout=20)
        if code == 0 and out:
            for line in out.splitlines():
                if "Current balance:" in line:
                    try:
                        val = line.split("Current balance:")[1].split()[0]
                        self.compute_balance = float(val)
                    except Exception:
                        pass
                elif "Usage rate:" in line:
                    try:
                        val = line.split("Usage rate:")[1].split("/")[0].strip()
                        self.compute_burn_rate = float(val)
                    except Exception:
                        pass

    async def _run_colab_cmd(
        self, args: list[str], timeout: float = 120, input_text: Optional[str] = None
    ) -> tuple[int, str, str]:
        if args and args[0] == "exec" and not any(a.startswith("--timeout") for a in args):
            args = [args[0], f"--timeout={max(30, int(timeout - 5))}"] + args[1:]
        cmd = ["colab", f"--auth={config.colab_auth_mode}"] + args
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=input_text.encode() if input_text else None),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "", f"Command timed out after {timeout}s"
        out = stdout_b.decode("utf-8", errors="replace").strip()
        err = stderr_b.decode("utf-8", errors="replace").strip()
        return proc.returncode or 0, out, err

    async def _list_colab_sessions(self) -> dict[str, dict]:
        code, out, err = await self._run_colab_cmd(["sessions"], timeout=30)
        sessions_path = Path(os.path.expanduser("~/.config/colab-cli/sessions.json"))
        if sessions_path.exists():
            try:
                return json.loads(sessions_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    async def check_local_comfy_health(self) -> tuple[bool, int, int]:
        """Checks if ComfyUI is reachable through the local SSH tunnel and returns (healthy, running, pending)."""
        url = f"http://127.0.0.1:{config.local_comfy_port}/queue"
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    running = len(data.get("queue_running", []))
                    pending = len(data.get("queue_pending", []))
                    return True, running, pending
        except Exception:
            pass
        return False, 0, 0

    async def ensure_started(self, requested_gpu: Optional[str] = None) -> bool:
        """Triggers boot if not running or already booting, and waits or returns status."""
        self.touch_activity("ensure_started")
        if self.phase == InstancePhase.READY:
            healthy, r, p = await self.check_local_comfy_health()
            if healthy:
                self.running_jobs_count = r
                self.queued_jobs_count = p
                return True
            self._log("本地隧道或 ComfyUI 健康检查未通过，准备自动重连/恢复实例...")

        if self._boot_task and not self._boot_task.done():
            return False

        self._boot_task = asyncio.create_task(self._boot_flow(requested_gpu=requested_gpu))
        return False

    async def wait_until_ready(self, timeout: float = 1200) -> bool:
        """Waits until the instance reaches READY state (used by synchronous/blocking API calls)."""
        self.touch_activity("api_wait")
        await self.ensure_started()
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.touch_activity("api_waiting_boot")
            if self.phase == InstancePhase.READY:
                healthy, _, _ = await self.check_local_comfy_health()
                if healthy:
                    return True
            if self.phase == InstancePhase.ERROR:
                raise RuntimeError(self.error_message or "Failed to provision Colab ComfyUI instance")
            if self.phase == InstancePhase.STOPPED and (not self._boot_task or self._boot_task.done()):
                await self.ensure_started()
            await asyncio.sleep(2.0)
        raise TimeoutError(f"Timed out waiting for Colab ComfyUI instance after {timeout}s")

    async def _boot_flow(self, requested_gpu: Optional[str] = None) -> None:
        async with self._boot_lock:
            self.error_message = None
            self.drive_auth_url = None
            self.touch_activity("boot_start")

            try:
                # Step 1: Check or Create Colab Session
                self.phase = InstancePhase.ALLOCATING_VM
                self.phase_detail = "正在查询云端活跃实例..."
                self._log("开始实例生命周期检查...")

                sessions = await self._list_colab_sessions()
                sname = config.colab_session_name

                if sname not in sessions:
                    gpu_candidates = []
                    target = requested_gpu or config.default_gpu
                    gpu_candidates.append(target)
                    for g in config.fallback_gpus:
                        if g not in gpu_candidates:
                            gpu_candidates.append(g)

                    allocated = False
                    last_err = ""
                    for gpu in gpu_candidates:
                        self.phase_detail = f"正在申请 Google Colab GPU 实例 ({gpu})..."
                        self._log(f"正在创建 Colab 会话 '{sname}' (请求 GPU: {gpu})...")
                        code, out, err = await self._run_colab_cmd(
                            ["new", "-s", sname, "--gpu", gpu], timeout=90
                        )
                        if code == 0:
                            allocated = True
                            self.active_gpu = gpu
                            self._log(f"成功分配 Colab GPU 实例: {gpu}")
                            break
                        else:
                            last_err = f"{out} {err}".strip()
                            self._log(f"申请 GPU {gpu} 未成功: {last_err}，尝试备选型号...")

                    if not allocated:
                        raise RuntimeError(f"无法分配 Colab GPU 实例: {last_err}")

                    sessions = await self._list_colab_sessions()

                s_info = sessions.get(sname, {})
                self.active_gpu = s_info.get("accelerator", self.active_gpu or config.default_gpu)
                self.endpoint = s_info.get("endpoint")

                # Propagate Google Drive persistent credentials first (takes ~0.5s via HTTP API, 0 kernel roundtrips)
                if config.enable_drive_mount:
                    await self._propagate_drive_credentials(sname)

                # Single All-In-One Streaming Bootstrap Execution (Eliminates 9 separate colab exec/upload roundtrips,
                # mounts Google Drive & installs ComfyUI in parallel threads, embeds workflow templates directly,
                # skips ComfyUI-Manager overhead by default, symlinks all 7 models, starts ComfyUI & background RAM prewarmer)
                await self._run_unified_bootstrap(sname)

                # Step 6: Start SSH Local Forward Tunnel & Healthcheck
                self.phase = InstancePhase.ESTABLISHING_TUNNEL
                self.phase_detail = "正在建立本地至 Colab 的安全 SSH 隧道..."
                await self._establish_ssh_tunnel(sname)

                self.started_at = self.started_at or time.time()
                self.touch_activity("boot_ready")
                self.phase = InstancePhase.READY
                self.phase_detail = f"运行中 ({self.active_gpu}) · 固定入口反代已连通"
                self._log("ComfyUI (MiniMax H3) 已完全就绪！")
                await self.refresh_usage()

            except Exception as e:
                self.phase = InstancePhase.ERROR
                self.error_message = str(e)
                self.phase_detail = f"启动失败: {e}"
                self._log(f"错误: {e}")

    async def _propagate_drive_credentials(self, sname: str) -> None:
        """Propagates dfs_persistent credentials to Colab backend via HTTP API (no VM kernel wait)."""
        prop_script = """
import json
from colab_cli.common import state
from colab_cli.auth import get_credentials
from colab_cli.utils import get_status_code

s = state.get_session(""" + json.dumps(sname) + """)
url = f"{state.client.colab_domain}/tun/m/credentials-propagation/{s.endpoint}"
creds = get_credentials(state.client_oauth_config, provider=state.auth_provider)

for authtype in ("dfs_persistent", "dfs_ephemeral"):
    params = {
        "authuser": "0",
        "authtype": authtype,
        "version": "2",
        "dryrun": "true",
        "propagate": "true",
        "record": "false",
    }
    resp = creds.request("GET", url, params=params)
    token = json.loads(resp.text.split("\\n", 1)[-1]).get("token") if get_status_code(resp) == 200 else None
    if token:
        headers = {"x-goog-colab-token": token}
        resp2 = creds.request("POST", url, params=params, headers=headers, files={"file_id": (None, "empty.ipynb")})
        data = json.loads(resp2.text.split("\\n", 1)[-1])
        if data.get("success"):
            params["dryrun"] = "false"
            resp3 = creds.request("POST", url, params=params, headers=headers, files={"file_id": (None, "empty.ipynb")})
            if get_status_code(resp3) == 200:
                print(f"PROPAGATED_OK:{authtype}")
                break
        elif authtype == "dfs_ephemeral" and data.get("unauthorized_redirect_uri"):
            print("NEED_AUTH_URI:" + str(data.get("unauthorized_redirect_uri", "")))
"""
        import sys

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            prop_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await proc.communicate()
        prop_out = stdout_b.decode("utf-8", errors="replace")
        if "PROPAGATED_OK:dfs_persistent" in prop_out:
            self.drive_auth_url = None
            self._log("已下发 Google Drive 持久授权凭据 (dfs_persistent)")
        elif "NEED_AUTH_URI:" in prop_out:
            for line in prop_out.splitlines():
                if line.startswith("NEED_AUTH_URI:"):
                    self.drive_auth_url = line.split("NEED_AUTH_URI:", 1)[1].strip()

    async def _run_unified_bootstrap(self, sname: str) -> None:
        """Runs the entire VM setup (fast-path check, parallel Drive mount + ComfyUI install, workflow embedding,
        model symlinking, ComfyUI startup, and background model RAM prewarming) in a single streaming colab exec call."""
        workflows_payload = {}
        for wf_name in (
            "video_minimax_h3_t2v.json",
            "video_minimax_h3_i2v.json",
            "video_minimax_h3_t2v_heretic.json",
            "video_minimax_h3_i2v_heretic.json",
        ):
            local_wf = Path("/app/app/templates") / wf_name
            if local_wf.exists():
                workflows_payload[wf_name] = local_wf.read_text(encoding="utf-8")

        models_to_fetch = [
            (config.hf_repo_id, "vae", f"vae/{config.video_vae_model}", config.video_vae_model),
            (config.hf_repo_id, "vae", f"vae/{config.audio_vae_model}", config.audio_vae_model),
            (config.hf_repo_id, "diffusion_models", f"diffusion_models/{config.unet_model}", config.unet_model),
            (config.hf_repo_id, "text_encoders", f"text_encoders/{config.official_clip_model}", config.official_clip_model),
            (config.heretic_repo_id, "text_encoders", config.heretic_clip_model, config.heretic_clip_model),
            (config.hf_repo_id, "loras", f"loras/{config.turbo_lora_8step}", config.turbo_lora_8step),
            (config.hf_repo_id, "loras", f"loras/{config.turbo_lora_4step}", config.turbo_lora_4step),
        ]

        import base64
        import zlib

        bootstrap_cfg = {
            "enable_drive": bool(config.enable_drive_mount),
            "drive_base": str(config.drive_base_dir),
            "install_mgr": bool(config.install_comfyui_manager),
            "prewarm_models": bool(config.prewarm_drive_models),
            "workflows": workflows_payload,
            "models": models_to_fetch,
        }
        payload_b64 = base64.b64encode(
            zlib.compress(json.dumps(bootstrap_cfg).encode("utf-8"))
        ).decode("ascii")

        unified_script = f'PAYLOAD_B64 = "{payload_b64}"\n' + r"""
import os, sys, json, time, zlib, base64, shutil, subprocess, urllib.request
from pathlib import Path

CFG = json.loads(zlib.decompress(base64.b64decode(PAYLOAD_B64)).decode("utf-8"))

def is_comfy_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=1.5)
        return True
    except Exception:
        return False

# 0. Fast-path check
if is_comfy_up():
    print("STAGE:STARTING_COMFYUI:检测到远端 ComfyUI 已在运行中，跳过安装步骤", flush=True)
    print("BOOTSTRAP_COMPLETE_OK", flush=True)
else:
    gpu_info = subprocess.getoutput("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader")
    print(f"LOG:远端 GPU 硬件确认: {gpu_info}", flush=True)

    enable_drive = CFG["enable_drive"]
    drive_base = Path(CFG["drive_base"])
    install_mgr = CFG["install_mgr"]
    prewarm_models = CFG["prewarm_models"]
    workflows_map = CFG["workflows"]
    models_list = CFG["models"]

    print("STAGE:MOUNTING_DRIVE:正在并行挂载 Google Drive 与安装 ComfyUI 环境...", flush=True)

    # Launch ComfyUI git clone + uv pip install as an independent OS process
    # so it runs in true parallel with IPython's main-thread drive._mount()
    install_py = '''import os, sys, subprocess, shutil, urllib.request
from pathlib import Path

comfy_dir = Path("/content/ComfyUI")
if not comfy_dir.exists():
    print("LOG:[VM] 正在克隆 ComfyUI 仓库...", flush=True)
    subprocess.run(["git", "clone", "--depth", "1", "https://github.com/comfyanonymous/ComfyUI.git", str(comfy_dir)], check=True)

print("STAGE:INSTALLING_COMFYUI:正在使用 uv 极速安装 ComfyUI 依赖...", flush=True)
req_file = comfy_dir / "requirements.txt"
try:
    subprocess.run(["uv", "pip", "install", "--system", "-q", "-r", str(req_file), "huggingface_hub[hf_transfer]", "hf_transfer", "accelerate", "safetensors", "einops", "av", "torchaudio"], check=True)
except Exception:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file), "huggingface_hub[hf_transfer]", "hf_transfer", "accelerate", "safetensors", "einops", "av", "torchaudio"], check=True)

in_dir = comfy_dir / "input"
in_dir.mkdir(parents=True, exist_ok=True)
sample_img = in_dir / "transparent_rgb_gaming_mouse.png"
if not sample_img.exists():
    try:
        urllib.request.urlretrieve("https://raw.githubusercontent.com/Comfy-Org/workflow_templates/refs/heads/main/input/transparent_rgb_gaming_mouse.png", str(sample_img))
    except Exception:
        pass
print("LOG:[VM] ComfyUI 核心环境安装完成", flush=True)
'''
    _ = Path("/tmp/install_comfy.py").write_text(install_py, encoding="utf-8")
    install_log_f = open("/tmp/install_comfy.log", "w")
    install_proc = subprocess.Popen(
        [sys.executable, "-u", "/tmp/install_comfy.py"],
        stdin=subprocess.DEVNULL,
        stdout=install_log_f,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )

    # Meanwhile on the main IPython thread, mount Google Drive silently
    if enable_drive:
        try:
            if not Path("/content/drive/MyDrive").exists():
                from google.colab import drive
                drive._mount("/content/drive", ephemeral=False)
            if Path("/content/drive/MyDrive").exists():
                (drive_base / "models").mkdir(parents=True, exist_ok=True)
                (drive_base / "outputs").mkdir(parents=True, exist_ok=True)
                print(f"LOG:Google Drive 免交互挂载就绪 ({drive_base})", flush=True)
        except Exception as e:
            print(f"LOG:Google Drive 挂载提示: {e}", flush=True)

    # Wait for ComfyUI background installer process and stream its logs
    read_pos = 0
    while install_proc.poll() is None:
        if os.path.exists("/tmp/install_comfy.log"):
            with open("/tmp/install_comfy.log", "r", encoding="utf-8", errors="ignore") as rf:
                rf.seek(read_pos)
                chunk = rf.read()
                read_pos = rf.tell()
                if chunk:
                    for ln in chunk.splitlines():
                        if ln.strip():
                            print(ln.strip(), flush=True)
        time.sleep(0.5)

    install_log_f.close()
    if os.path.exists("/tmp/install_comfy.log"):
        with open("/tmp/install_comfy.log", "r", encoding="utf-8", errors="ignore") as rf:
            rf.seek(read_pos)
            for ln in rf.read().splitlines():
                if ln.strip():
                    print(ln.strip(), flush=True)

    if install_proc.returncode != 0:
        raise RuntimeError(f"ComfyUI 安装进程退出码异常: {install_proc.returncode}")

    comfy_dir = Path("/content/ComfyUI")
    mgr_dir = comfy_dir / "custom_nodes" / "ComfyUI-Manager"
    if install_mgr and not mgr_dir.exists():
        subprocess.run(["git", "clone", "--depth", "1", "https://github.com/ltdrdata/ComfyUI-Manager.git", str(mgr_dir)], check=False)
    elif not install_mgr and mgr_dir.exists():
        shutil.rmtree(str(mgr_dir), ignore_errors=True)

    wf_dir = comfy_dir / "user" / "default" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    for wf_name, wf_content in workflows_map.items():
        _ = (wf_dir / wf_name).write_text(wf_content, encoding="utf-8")

    # 3. Symlink or download models
    print("STAGE:DOWNLOADING_MODELS:正在从 Google Drive 秒级软链全套 MiniMax H3 模型...", flush=True)
    from huggingface_hub import hf_hub_download
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
    use_drive = Path("/content/drive/MyDrive").exists()

    for repo_id, subdir, hf_path, filename in models_list:
        comfy_target_dir = Path("/content/ComfyUI/models") / subdir
        comfy_target_dir.mkdir(parents=True, exist_ok=True)
        target_file = comfy_target_dir / filename

        if use_drive:
            drive_model_dir = drive_base / "models" / subdir
            drive_model_dir.mkdir(parents=True, exist_ok=True)
            drive_file = drive_model_dir / filename

            if drive_file.exists() and drive_file.stat().st_size > 1024 * 1024:
                if not target_file.is_symlink() or os.readlink(str(target_file)) != str(drive_file):
                    if target_file.exists() or target_file.is_symlink():
                        target_file.unlink()
                    os.symlink(str(drive_file), str(target_file))
                print(f"LOG:[Model] LINKED_FROM_DRIVE: {filename}", flush=True)
            elif target_file.exists() and not target_file.is_symlink() and target_file.stat().st_size > 1024 * 1024:
                shutil.copy2(str(target_file), str(drive_file))
                target_file.unlink()
                os.symlink(str(drive_file), str(target_file))
                print(f"LOG:[Model] SYNCED_AND_LINKED: {filename}", flush=True)
            else:
                print(f"LOG:[Model] DOWNLOADING_TO_DRIVE: {filename}", flush=True)
                dl_dir = drive_model_dir if "/" not in hf_path else (drive_base / "models")
                hf_hub_download(repo_id=repo_id, filename=hf_path, local_dir=str(dl_dir))
                if target_file.exists() or target_file.is_symlink():
                    target_file.unlink()
                os.symlink(str(drive_file), str(target_file))
                print(f"LOG:[Model] LINKED_FROM_DRIVE: {filename}", flush=True)
        else:
            if not (target_file.exists() and target_file.stat().st_size > 1024 * 1024):
                print(f"LOG:[Model] DOWNLOADING_TO_VM: {filename}", flush=True)
                dl_dir = comfy_target_dir if "/" not in hf_path else Path("/content/ComfyUI/models")
                hf_hub_download(repo_id=repo_id, filename=hf_path, local_dir=str(dl_dir))

    # 4. Link /content/ComfyUI/output directly to Google Drive outputs folder & Start ComfyUI Server
    if use_drive:
        drive_out_dir = drive_base / "outputs"
        drive_out_dir.mkdir(parents=True, exist_ok=True)
        comfy_out_dir = Path("/content/ComfyUI/output")
        if comfy_out_dir.exists() and not comfy_out_dir.is_symlink():
            for p in comfy_out_dir.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(comfy_out_dir)
                    dest = drive_out_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        shutil.copy2(str(p), str(dest))
            shutil.rmtree(str(comfy_out_dir), ignore_errors=True)
        if not comfy_out_dir.exists() and not comfy_out_dir.is_symlink():
            os.symlink(str(drive_out_dir), str(comfy_out_dir))
        print(f"LOG:已将 ComfyUI 输出目录直接软链至 Google Drive ({drive_out_dir})", flush=True)

    print("STAGE:STARTING_COMFYUI:正在启动远端 ComfyUI 引擎 (已精简 Prestartup 开销)...", flush=True)
    log_f = open("/content/comfyui.log", "a")
    subprocess.Popen(
        ["python3", "main.py", "--listen", "127.0.0.1", "--port", "8188", "--enable-cors-header", "--disable-auto-launch"],
        cwd="/content/ComfyUI",
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
    )

    for _ in range(180):
        if is_comfy_up():
            print("LOG:远端 ComfyUI 进程已在 127.0.0.1:8188 启动成功", flush=True)
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("ComfyUI 启动超时")

    # 5. Optional background Drive model RAM prewarmer so first video generation doesn't wait on FUSE read
    if prewarm_models and use_drive:
        prewarm_py = '''
import os
files = [
    "/content/ComfyUI/models/vae/minimax_h3_video_vae_int8_convrot.safetensors",
    "/content/ComfyUI/models/vae/minimax_h3_audio_vae_fp32.safetensors",
    "/content/ComfyUI/models/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
    "/content/ComfyUI/models/text_encoders/qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors",
    "/content/ComfyUI/models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
]
for fp in files:
    if os.path.exists(fp):
        try:
            with open(fp, "rb") as f:
                while f.read(16 * 1024 * 1024):
                    pass
        except Exception:
            pass
'''
        subprocess.Popen(
            ["python3", "-c", prewarm_py],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        print("LOG:已启动后台内存预热任务 (将云盘模型预读至 Linux RAM PageCache)", flush=True)

    print("BOOTSTRAP_COMPLETE_OK", flush=True)
"""

        cmd = [
            "python3",
            "-u",
            "/usr/local/bin/colab",
            f"--auth={config.colab_auth_mode}",
            "exec",
            "--timeout=1800",
            "-s",
            sname,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=16 * 1024 * 1024,
        )
        assert proc.stdin is not None
        proc.stdin.write(unified_script.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        assert proc.stdout is not None
        success = False
        collected_lines: list[str] = []
        while True:
            line_b = await proc.stdout.readline()
            if not line_b:
                break
            line = line_b.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            collected_lines.append(line)
            if line.startswith("STAGE:"):
                parts = line.split(":", 2)
                if len(parts) == 3:
                    _, stage_name, detail_msg = parts
                    try:
                        self.phase = InstancePhase(stage_name)
                    except Exception:
                        pass
                    self.phase_detail = detail_msg
                    self._log(detail_msg)
            elif line.startswith("LOG:"):
                self._log(line[4:])
            elif "BOOTSTRAP_COMPLETE_OK" in line:
                success = True

        await proc.wait()
        if not success:
            tail = "\n".join(collected_lines[-20:])
            raise RuntimeError(f"一体化引导部署未完成: {tail}")

    async def _try_mount_drive(self, sname: str) -> None:
        """Attempts to mount Google Drive automatically using dfs_persistent (no browser consent popup) or dfs_ephemeral fallback."""
        check_script = f"""
import os, json
from pathlib import Path

mount_point = Path("/content/drive/MyDrive")
if mount_point.exists():
    base = Path({json.dumps(config.drive_base_dir)})
    (base / "models").mkdir(parents=True, exist_ok=True)
    (base / "outputs").mkdir(parents=True, exist_ok=True)
    print("DRIVE_MOUNT_OK")
else:
    print("DRIVE_NOT_MOUNTED")
"""
        code, out, err = await self._run_colab_cmd(["exec", "-s", sname], timeout=30, input_text=check_script)
        if "DRIVE_MOUNT_OK" in out:
            self._log(f"Google Drive 已挂载 ({config.drive_base_dir})")
            return

        # 1. Try persistent credentials propagation (dfs_persistent) so no browser confirmation is needed across sessions
        prop_script = """
import json
from colab_cli.common import state
from colab_cli.auth import get_credentials
from colab_cli.utils import get_status_code

s = state.get_session(""" + json.dumps(sname) + """)
url = f"{state.client.colab_domain}/tun/m/credentials-propagation/{s.endpoint}"
creds = get_credentials(state.client_oauth_config, provider=state.auth_provider)

# First try dfs_persistent (does not require per-VM browser consent!)
for authtype in ("dfs_persistent", "dfs_ephemeral"):
    params = {
        "authuser": "0",
        "authtype": authtype,
        "version": "2",
        "dryrun": "true",
        "propagate": "true",
        "record": "false",
    }
    resp = creds.request("GET", url, params=params)
    token = json.loads(resp.text.split("\\n", 1)[-1]).get("token") if get_status_code(resp) == 200 else None
    if token:
        headers = {"x-goog-colab-token": token}
        resp2 = creds.request("POST", url, params=params, headers=headers, files={"file_id": (None, "empty.ipynb")})
        data = json.loads(resp2.text.split("\\n", 1)[-1])
        if data.get("success"):
            params["dryrun"] = "false"
            resp3 = creds.request("POST", url, params=params, headers=headers, files={"file_id": (None, "empty.ipynb")})
            if get_status_code(resp3) == 200:
                print(f"PROPAGATED_OK:{authtype}")
                break
        elif authtype == "dfs_ephemeral" and data.get("unauthorized_redirect_uri"):
            print("NEED_AUTH_URI:" + str(data.get("unauthorized_redirect_uri", "")))
"""
        import sys

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            prop_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await proc.communicate()
        prop_out = stdout_b.decode("utf-8", errors="replace")

        if "PROPAGATED_OK:dfs_persistent" in prop_out:
            self._log("已通过持久凭据 (dfs_persistent) 自动下发 Google Drive 授权，正在免交互挂载...")
            mount_cmd = (
                "from google.colab import drive\n"
                "drive._mount('/content/drive', ephemeral=False)\n"
            ) + check_script
            _, m_out, m_err = await self._run_colab_cmd(["exec", "-s", sname], timeout=120, input_text=mount_cmd)
            if "DRIVE_MOUNT_OK" in m_out or "Mounted at" in m_out:
                self.drive_auth_url = None
                self._log(f"Google Drive 免交互自动挂载成功 ({config.drive_base_dir})")
                return
            self._log(f"持久模式挂载反馈: {m_out} {m_err}")

        if "PROPAGATED_OK:dfs_ephemeral" in prop_out:
            self._log("Google Drive 临时授权就绪，正在自动挂载 /content/drive ...")
            await self._run_colab_cmd(["drivemount", "-s", sname], timeout=90, input_text="\n")
            _, m_out, _ = await self._run_colab_cmd(["exec", "-s", sname], timeout=30, input_text=check_script)
            if "DRIVE_MOUNT_OK" in m_out:
                self.drive_auth_url = None
                self._log("Google Drive 自动挂载完成")
                return

        if "NEED_AUTH_URI:" in prop_out:
            for line in prop_out.splitlines():
                if line.startswith("NEED_AUTH_URI:"):
                    self.drive_auth_url = line.split("NEED_AUTH_URI:", 1)[1].strip()
            self._log("提示: Google Drive 首次云端挂载需要浏览器确认一次授权（已在面板提供授权链接），本次将使用实例高速存储 + 本机持久化同步。")
        else:
            self._log("提示: 未检测到 Google Drive 免交互凭据，使用实例本地存储 + 本机持久化目录。")

    async def complete_drive_mount(self) -> tuple[bool, str]:
        """Called when the user clicks 'I have authorized Google Drive' in the UI."""
        sname = config.colab_session_name
        self._log("正在完成 Google Drive 授权传播与云盘挂载...")
        mount_script = f"""
import json, sys
from colab_cli.common import state
from colab_cli.auth import get_credentials
from colab_cli.utils import get_status_code
from colab_cli.runtime import ColabRuntime

s = state.get_session({json.dumps(sname)})
url = f"{{state.client.colab_domain}}/tun/m/credentials-propagation/{{s.endpoint}}"
creds = get_credentials(state.client_oauth_config, provider=state.auth_provider)

params = {{
    "authuser": "0",
    "authtype": "dfs_ephemeral",
    "version": "2",
    "dryrun": "true",
    "propagate": "true",
    "record": "false",
}}
resp = creds.request("GET", url, params=params)
token = json.loads(resp.text.split("\\n", 1)[-1]).get("token") if get_status_code(resp) == 200 else None
if token:
    headers = {{"x-goog-colab-token": token}}
    params["dryrun"] = "false"
    creds.request("POST", url, params=params, headers=headers, files={{"file_id": (None, "empty.ipynb")}})

runtime = ColabRuntime(s.url, s.token, session_name=s.name, history=state.history)
def hook(deserialize_msg, wsclient):
    content = deserialize_msg.get("content", {{}})
    if content.get("request", {{}}).get("authType") == "dfs_ephemeral":
        msg_id = deserialize_msg.get("metadata", {{}}).get("colab_msg_id")
        if token:
            creds.request("POST", url, params=params, headers=headers, files={{"file_id": (None, "empty.ipynb")}})
        reply = wsclient.session.msg("input_reply", {{"value": {{"type": "colab_reply", "colab_msg_id": msg_id}}}})
        if "header" in deserialize_msg:
            reply["parent_header"] = deserialize_msg["header"]
        wsclient.stdin_channel.send(reply)
        return True
    return False

runtime.colab_request_hook = hook
try:
    outs = runtime.execute_code("from google.colab import drive\\ndrive.mount('/content/drive')\\nimport os\\nif os.path.exists('/content/drive/MyDrive'): print('DRIVE_MOUNTED_SUCCESS')\\n", allow_stdin=True, timeout=60)
    for o in outs:
        if 'text' in o:
            print(o['text'])
finally:
    runtime.stop()
"""
        import sys

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            mount_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        out = stdout_b.decode("utf-8", errors="replace").strip()
        err = stderr_b.decode("utf-8", errors="replace").strip()
        if "DRIVE_MOUNTED_SUCCESS" in out or "Mounted at" in out or "already mounted" in out:
            self.drive_auth_url = None
            self._log("Google Drive 挂载成功！")
            return True, "Google Drive 挂载成功！"
        self._log(f"Google Drive 挂载状态: {out} {err}")
        return False, f"挂载尚未就绪，请确认已在打开的 Google 页面点击允许: {out or err}"

    async def _deploy_comfyui(self, sname: str) -> None:
        deploy_script = f"""
import os, subprocess, sys, json, shutil
from pathlib import Path

comfy_dir = Path("/content/ComfyUI")
if not comfy_dir.exists():
    print("Cloning ComfyUI repository...", flush=True)
    subprocess.run(["git", "clone", "--depth", "1", "https://github.com/comfyanonymous/ComfyUI.git", str(comfy_dir)], check=True)
else:
    print("ComfyUI repository already exists, updating...", flush=True)
    subprocess.run(["git", "-C", str(comfy_dir), "pull", "--ff-only"], check=False)

print("Installing ComfyUI Python dependencies...", flush=True)
req_file = comfy_dir / "requirements.txt"
try:
    subprocess.run(["uv", "pip", "install", "--system", "-r", str(req_file), "huggingface_hub[hf_transfer]", "hf_transfer", "accelerate", "safetensors", "einops", "av", "torchaudio"], check=True)
except Exception:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file), "huggingface_hub[hf_transfer]", "hf_transfer", "accelerate", "safetensors", "einops", "av", "torchaudio"], check=True)

# Install ComfyUI-Manager for convenience
mgr_dir = comfy_dir / "custom_nodes" / "ComfyUI-Manager"
if not mgr_dir.exists():
    subprocess.run(["git", "clone", "--depth", "1", "https://github.com/ltdrdata/ComfyUI-Manager.git", str(mgr_dir)], check=False)

# Pre-populate default MiniMax H3 workflows in user/default/workflows so user sees them immediately in UI
wf_dir = comfy_dir / "user" / "default" / "workflows"
wf_dir.mkdir(parents=True, exist_ok=True)
in_dir = comfy_dir / "input"
in_dir.mkdir(parents=True, exist_ok=True)
sample_img = in_dir / "transparent_rgb_gaming_mouse.png"
if not sample_img.exists():
    try:
        import urllib.request
        urllib.request.urlretrieve("https://raw.githubusercontent.com/Comfy-Org/workflow_templates/refs/heads/main/input/transparent_rgb_gaming_mouse.png", str(sample_img))
    except Exception:
        pass
print("COMFYUI_DEPLOY_OK", flush=True)
"""
        self._log("正在远端实例安装/更新 ComfyUI 及依赖...")
        code, out, err = await self._run_colab_cmd(
            ["exec", "-s", sname], timeout=600, input_text=deploy_script
        )
        for line in out.splitlines():
            if line.strip():
                self._log(f"[VM] {line.strip()}")
        if code != 0 or "COMFYUI_DEPLOY_OK" not in out:
            raise RuntimeError(f"ComfyUI 部署失败: {err or out}")

        # Upload default MiniMax H3 workflows to ComfyUI user workflows folder
        for wf_name in (
            "video_minimax_h3_t2v.json",
            "video_minimax_h3_i2v.json",
            "video_minimax_h3_t2v_heretic.json",
            "video_minimax_h3_i2v_heretic.json",
        ):
            local_wf = Path("/app/app/templates") / wf_name
            if local_wf.exists():
                await self._run_colab_cmd(
                    [
                        "upload",
                        "-s",
                        sname,
                        str(local_wf),
                        f"/content/ComfyUI/user/default/workflows/{wf_name}",
                    ],
                    timeout=30,
                )

    async def _prepare_models(self, sname: str) -> None:
        models_to_fetch = [
            (config.hf_repo_id, "vae", f"vae/{config.video_vae_model}", config.video_vae_model),
            (config.hf_repo_id, "vae", f"vae/{config.audio_vae_model}", config.audio_vae_model),
            (config.hf_repo_id, "diffusion_models", f"diffusion_models/{config.unet_model}", config.unet_model),
            (config.hf_repo_id, "text_encoders", f"text_encoders/{config.official_clip_model}", config.official_clip_model),
            (config.heretic_repo_id, "text_encoders", config.heretic_clip_model, config.heretic_clip_model),
            (config.hf_repo_id, "loras", f"loras/{config.turbo_lora_8step}", config.turbo_lora_8step),
            (config.hf_repo_id, "loras", f"loras/{config.turbo_lora_4step}", config.turbo_lora_4step),
        ]

        self.phase_detail = "正在从 Google Drive 软链或同步全套 MiniMax H3 模型 (含 Heretic NVFP4)..."
        self._log("开始批量检查/软链 Google Drive 中的 MiniMax H3 模型 (含 Heretic NVFP4)...")

        batch_script = f"""
import os, shutil, json
from pathlib import Path
from huggingface_hub import hf_hub_download

os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
drive_base = Path({json.dumps(config.drive_base_dir)})
use_drive = Path("/content/drive/MyDrive").exists()
models = {json.dumps(models_to_fetch)}

for repo_id, subdir, hf_path, filename in models:
    comfy_target_dir = Path("/content/ComfyUI/models") / subdir
    comfy_target_dir.mkdir(parents=True, exist_ok=True)
    target_file = comfy_target_dir / filename

    if use_drive:
        drive_model_dir = drive_base / "models" / subdir
        drive_model_dir.mkdir(parents=True, exist_ok=True)
        drive_file = drive_model_dir / filename

        if drive_file.exists() and drive_file.stat().st_size > 1024 * 1024:
            if not target_file.is_symlink() or os.readlink(str(target_file)) != str(drive_file):
                if target_file.exists() or target_file.is_symlink():
                    target_file.unlink()
                os.symlink(str(drive_file), str(target_file))
            print(f"LINKED_FROM_DRIVE: {{filename}}", flush=True)
        elif target_file.exists() and not target_file.is_symlink() and target_file.stat().st_size > 1024 * 1024:
            print(f"SYNCING_VM_TO_DRIVE: {{filename}}", flush=True)
            shutil.copy2(str(target_file), str(drive_file))
            target_file.unlink()
            os.symlink(str(drive_file), str(target_file))
            print(f"LINKED_FROM_DRIVE: {{filename}}", flush=True)
        else:
            print(f"DOWNLOADING_TO_DRIVE: {{filename}} (from {{repo_id}})", flush=True)
            dl_dir = drive_model_dir if "/" not in hf_path else (drive_base / "models")
            hf_hub_download(repo_id=repo_id, filename=hf_path, local_dir=str(dl_dir))
            if target_file.exists() or target_file.is_symlink():
                target_file.unlink()
            os.symlink(str(drive_file), str(target_file))
            print(f"LINKED_FROM_DRIVE: {{filename}}", flush=True)
    else:
        if target_file.exists() and target_file.stat().st_size > 1024 * 1024:
            print(f"SKIP_EXISTING: {{filename}}", flush=True)
        else:
            print(f"DOWNLOADING_TO_VM: {{filename}} (from {{repo_id}})", flush=True)
            dl_dir = comfy_target_dir if "/" not in hf_path else Path("/content/ComfyUI/models")
            hf_hub_download(repo_id=repo_id, filename=hf_path, local_dir=str(dl_dir))

print("ALL_MODELS_READY_OK", flush=True)
"""
        code, out, err = await self._run_colab_cmd(
            ["exec", "-s", sname], timeout=1800, input_text=batch_script
        )
        for line in out.splitlines():
            if line.strip():
                self._log(f"[Model] {line.strip()}")
        if code != 0 and "ALL_MODELS_READY_OK" not in out:
            raise RuntimeError(f"模型准备失败: {err or out}")

    async def _start_remote_comfyui(self, sname: str) -> None:
        start_script = """
import subprocess, time, urllib.request, os

def is_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=2)
        return True
    except Exception:
        return False

if not is_up():
    log_f = open("/content/comfyui.log", "a")
    subprocess.Popen(
        ["python3", "main.py", "--listen", "127.0.0.1", "--port", "8188", "--enable-cors-header"],
        cwd="/content/ComfyUI",
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

for i in range(60):
    if is_up():
        print("REMOTE_COMFYUI_UP", flush=True)
        break
    time.sleep(2)
else:
    print("REMOTE_COMFYUI_TIMEOUT", flush=True)
    if os.path.exists("/content/comfyui.log"):
        print(open("/content/comfyui.log").read()[-2000:], flush=True)
"""
        code, out, err = await self._run_colab_cmd(
            ["exec", "-s", sname], timeout=180, input_text=start_script
        )
        if "REMOTE_COMFYUI_UP" not in out:
            raise RuntimeError(f"远端 ComfyUI 启动超时: {out} {err}")
        self._log("远端 ComfyUI 进程已在 127.0.0.1:8188 启动成功")

    async def _establish_ssh_tunnel(self, sname: str) -> None:
        await self._kill_tunnel()

        proxy_cmd = (
            f"colab --auth={config.colab_auth_mode} ssh --proxy-mode "
            f"-s {shlex.quote(sname)} -i {shlex.quote(str(self.ssh_key_path))}"
        )
        ssh_cmd = [
            "ssh",
            "-N",
            "-L",
            f"127.0.0.1:{config.local_comfy_port}:127.0.0.1:8188",
            "-i",
            str(self.ssh_key_path),
            "-o",
            f"ProxyCommand={proxy_cmd}",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "LogLevel=ERROR",
            "root@colab-runtime",
        ]

        self._log(f"正在启动 SSH 端口转发隧道 (127.0.0.1:{config.local_comfy_port} -> Colab:8188)...")
        self._tunnel_proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        # Wait up to 45 seconds for local forwarded port to pass ComfyUI health check
        for i in range(150):
            if self._tunnel_proc.returncode is not None:
                _, stderr_b = await self._tunnel_proc.communicate()
                err_txt = stderr_b.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"SSH 隧道进程异常退出: {err_txt}")

            healthy, r, p = await self.check_local_comfy_health()
            if healthy:
                self.running_jobs_count = r
                self.queued_jobs_count = p
                self._log("SSH 反代隧道已连通并通过健康检查！")
                return
            await asyncio.sleep(0.3)

        await self._kill_tunnel()
        raise RuntimeError("SSH 隧道健康检查超时，未能连接到远端 ComfyUI 端口")

    async def _kill_tunnel(self) -> None:
        if self._tunnel_proc and self._tunnel_proc.returncode is None:
            try:
                os.killpg(os.getpgid(self._tunnel_proc.pid), 15)
            except Exception:
                try:
                    self._tunnel_proc.terminate()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self._tunnel_proc.wait(), timeout=3.0)
            except Exception:
                try:
                    os.killpg(os.getpgid(self._tunnel_proc.pid), 9)
                except Exception:
                    try:
                        self._tunnel_proc.kill()
                    except Exception:
                        pass
        self._tunnel_proc = None
        # Ensure any orphaned ssh / colab ssh --proxy-mode processes holding port 18188 are cleaned up
        try:
            for p in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    cmd = p.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
                    pid = int(p.parent.name)
                    if pid != os.getpid() and ("18188:127.0.0.1:8188" in cmd or "ssh --proxy-mode" in cmd):
                        os.kill(pid, 9)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(0.5)

    async def sync_outputs_from_remote(self) -> list[str]:
        """Ensures all generated outputs on Colab are saved & flushed to Google Drive (no local VPS video storage)."""
        sname = config.colab_session_name
        manifest_file = config.data_dir / "drive_outputs.json"

        list_script = f"""
import os, json, shutil
from pathlib import Path

out_root = Path("/content/ComfyUI/output")
drive_base = Path({json.dumps(config.drive_base_dir)})
use_drive = Path("/content/drive/MyDrive").exists()
drive_out_root = drive_base / "outputs"

if use_drive:
    drive_out_root.mkdir(parents=True, exist_ok=True)

files = []
scan_root = drive_out_root if (use_drive and out_root.is_symlink()) else out_root
if scan_root.exists():
    for p in scan_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".mp4", ".webm", ".gif", ".png", ".jpg", ".wav", ".mp3", ".flac"):
            rel = str(p.relative_to(scan_root))
            drive_target = drive_out_root / rel
            if use_drive and not out_root.is_symlink():
                drive_target.parent.mkdir(parents=True, exist_ok=True)
                if not drive_target.exists() or drive_target.stat().st_size != p.stat().st_size:
                    shutil.copy2(p, drive_target)
            files.append({{
                "rel": rel,
                "drive_path": str(drive_target) if use_drive else str(p),
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            }})

try:
    os.sync()
except Exception:
    pass

print("OUTPUT_MANIFEST:" + json.dumps(files), flush=True)
"""
        code, out, err = await self._run_colab_cmd(
            ["exec", "-s", sname], timeout=120, input_text=list_script
        )
        synced: list[str] = []
        if code != 0:
            return synced

        for line in out.splitlines():
            if line.startswith("OUTPUT_MANIFEST:"):
                try:
                    items = json.loads(line.split("OUTPUT_MANIFEST:", 1)[1])
                    manifest_file.parent.mkdir(parents=True, exist_ok=True)
                    manifest_file.write_text(
                        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    synced = [item["rel"] for item in items]
                except Exception as e:
                    self._log(f"解析 Google Drive 输出清单异常: {e}")
        return synced

    async def stop_instance(self, reason: str = "手动关闭") -> None:
        async with self._boot_lock:
            if self.phase == InstancePhase.STOPPED:
                return
            self.phase = InstancePhase.STOPPING
            self.phase_detail = f"正在同步保存生成结果并释放 GPU 实例 ({reason})..."
            self._log(f"触发实例关闭流程 ({reason})，正在保存生成结果...")

            try:
                await self.sync_outputs_from_remote()
            except Exception as e:
                self._log(f"同步输出结果警告: {e}")

            await self._kill_tunnel()

            sname = config.colab_session_name
            self._log(f"正在向 Colab 发送释放实例指令 (colab stop -s {sname})...")
            await self._run_colab_cmd(["stop", "-s", sname], timeout=60)

            self.phase = InstancePhase.STOPPED
            self.phase_detail = f"已安全释放 ({reason})"
            self.started_at = None
            self.endpoint = None
            self.running_jobs_count = 0
            self.queued_jobs_count = 0
            self._log(f"Colab GPU 实例已成功释放 ({reason})，不再产生算力费用。")
            await self.refresh_usage()

    async def _lifecycle_loop(self) -> None:
        """Background loop that checks tunnel health, ComfyUI queue activity, output syncing, and idle auto-shutdown."""
        last_usage_check = 0.0
        last_output_sync = 0.0

        while True:
            try:
                await asyncio.sleep(5.0)
                now = time.time()

                if now - last_usage_check > 180:
                    last_usage_check = now
                    await self.refresh_usage()

                if self.phase == InstancePhase.READY:
                    healthy, running, pending = await self.check_local_comfy_health()
                    self.running_jobs_count = running
                    self.queued_jobs_count = pending

                    if not healthy:
                        # Tunnel might have dropped or VM might have been reclaimed by Colab
                        self._log("检测到本地隧道连接断开，正在检查远端会话状态...")
                        sessions = await self._list_colab_sessions()
                        if config.colab_session_name in sessions:
                            try:
                                await self._start_remote_comfyui(config.colab_session_name)
                                await self._establish_ssh_tunnel(config.colab_session_name)
                                continue
                            except Exception as e:
                                self._log(f"自动重连隧道失败: {e}")
                        self._log("远端 Colab 实例已被回收或断开，状态切换为 STOPPED（下次访问将自动重建）")
                        await self._kill_tunnel()
                        self.phase = InstancePhase.STOPPED
                        self.phase_detail = "远端实例已回收，等待下次访问自动唤醒"
                        self.started_at = None
                        continue

                    if running > 0 or pending > 0 or self.active_api_tasks > 0:
                        self.touch_activity("comfy_queue_busy")

                    # Periodically sync new outputs while ready
                    if now - last_output_sync > 45 and running == 0:
                        last_output_sync = now
                        asyncio.create_task(self.sync_outputs_from_remote())

                    # Check idle timeout
                    if (
                        running == 0
                        and pending == 0
                        and self.active_api_tasks == 0
                        and self.idle_seconds >= config.idle_timeout_seconds
                    ):
                        await self.stop_instance(
                            reason=f"无运行/排队任务且闲置已达 {config.idle_timeout_seconds // 60} 分钟"
                        )
            except Exception as e:
                logger.debug(f"Lifecycle monitor error: {e}")


manager = ColabLifecycleManager()
