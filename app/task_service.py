import asyncio
import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from app.colab_manager import manager
from app.config import config
from app.workflow_builder import build_minimax_h3_api_prompt


class VideoGenerateRequest(BaseModel):
    prompt: str = Field(..., description="描述画面镜头、动作与伴随立体声音频（语音/音效/配乐）的提示词")
    width: int = Field(1344, description="视频宽度（会自动对齐 32 的倍数，推荐 1344 或 864）")
    height: int = Field(768, description="视频高度（会自动对齐 32 的倍数，推荐 768 或 480）")
    duration: float = Field(5.0, description="视频时长（秒，支持 2~15 秒，24fps）")
    seed: Optional[int] = Field(None, description="随机种子，不填则随机生成")
    turbo_mode: bool = Field(True, description="是否启用 Turbo LoRA 加速（默认开启 8 步极速生成）")
    steps: Optional[int] = Field(None, description="采样步数（Turbo 模式默认 8 步，标准模式默认 20 步）")
    first_frame_image: Optional[str] = Field(None, description="首帧图片 URL 或 Base64 数据（可选，用于图生视频 I2V）")
    last_frame_image: Optional[str] = Field(None, description="尾帧图片 URL 或 Base64 数据（可选，用于首尾帧视频 FL2V）")
    text_encoder: Optional[str] = Field(
        "heretic",
        description="文本编码器选择: 'heretic' (qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors) 或 'official' (qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors)",
    )
    workflow: Optional[dict] = Field(None, description="可选：直接传入自定义 ComfyUI API Prompt JSON 字典")
    wait: bool = Field(True, description="是否同步等待视频生成完成再返回（false 则立即返回 task_id 异步轮询）")


class TaskRecord(BaseModel):
    task_id: str
    status: str  # pending_instance, uploading_inputs, queued, running, completed, failed
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    prompt_id: Optional[str] = None
    request_summary: dict = {}
    output_files: list[str] = []
    video_url: Optional[str] = None
    error: Optional[str] = None
    progress_message: str = "任务已创建"


class TaskExecutionService:
    def __init__(self) -> None:
        self.tasks: dict[str, TaskRecord] = {}
        self.tasks_file = config.data_dir / "api_tasks.json"
        self._load_tasks()

    def _load_tasks(self) -> None:
        if self.tasks_file.exists():
            try:
                raw = json.loads(self.tasks_file.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    self.tasks[k] = TaskRecord(**v)
            except Exception:
                pass

    def _save_tasks(self) -> None:
        try:
            self.tasks_file.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v.model_dump() for k, v in list(self.tasks.items())[-200:]}
            self.tasks_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    async def submit_task(self, req: VideoGenerateRequest) -> TaskRecord:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        rec = TaskRecord(
            task_id=task_id,
            status="pending_instance",
            created_at=time.time(),
            request_summary={
                "prompt": req.prompt[:160] + ("..." if len(req.prompt) > 160 else ""),
                "width": req.width,
                "height": req.height,
                "duration": req.duration,
                "turbo_mode": req.turbo_mode,
            },
            progress_message="正在检查并唤醒 Colab GPU 实例...",
        )
        self.tasks[task_id] = rec
        self._save_tasks()

        runner = asyncio.create_task(self._execute_task(rec, req))
        if req.wait:
            await runner
        return rec

    async def _upload_image_to_comfy(self, img_source: str, label: str) -> str:
        """Downloads URL or decodes Base64 image and uploads it to local ComfyUI /upload/image endpoint."""
        raw_bytes: bytes
        ext = ".png"
        if img_source.startswith("http://") or img_source.startswith("https://"):
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                r = await client.get(img_source)
                r.raise_for_status()
                raw_bytes = r.content
                ct = r.headers.get("content-type", "")
                if "jpeg" in ct or "jpg" in ct:
                    ext = ".jpg"
                elif "webp" in ct:
                    ext = ".webp"
        else:
            b64_data = img_source
            if "," in b64_data and b64_data.startswith("data:"):
                header, b64_data = b64_data.split(",", 1)
                if "jpeg" in header or "jpg" in header:
                    ext = ".jpg"
                elif "webp" in header:
                    ext = ".webp"
            raw_bytes = base64.b64decode(b64_data)

        fname = f"{label}_{uuid.uuid4().hex[:8]}{ext}"
        url = f"http://127.0.0.1:{config.local_comfy_port}/upload/image"
        async with httpx.AsyncClient(timeout=30.0) as client:
            files = {"image": (fname, raw_bytes, f"image/{ext.lstrip('.')}")}
            data = {"overwrite": "true"}
            resp = await client.post(url, files=files, data=data)
            resp.raise_for_status()
            res_json = resp.json()
            return res_json.get("name", fname)

    async def _execute_task(self, rec: TaskRecord, req: VideoGenerateRequest) -> None:
        manager.active_api_tasks += 1
        manager.touch_activity("api_task_start")
        try:
            # 1. Ensure Colab instance and ComfyUI are booted and healthy
            rec.status = "pending_instance"
            rec.progress_message = "正在自动检查/启动 Colab GPU 实例并部署 ComfyUI..."
            self._save_tasks()

            await manager.wait_until_ready(timeout=1500)
            rec.started_at = time.time()

            # 2. Upload optional input images if provided
            first_frame_name = None
            last_frame_name = None
            if req.first_frame_image or req.last_frame_image:
                rec.status = "uploading_inputs"
                rec.progress_message = "正在上传参考帧图像到 ComfyUI..."
                self._save_tasks()
                if req.first_frame_image:
                    first_frame_name = await self._upload_image_to_comfy(req.first_frame_image, "first")
                if req.last_frame_image:
                    last_frame_name = await self._upload_image_to_comfy(req.last_frame_image, "last")

            # 3. Build workflow prompt
            prefix = f"video/MiniMax_H3_{rec.task_id}"
            if req.workflow:
                prompt_graph = req.workflow
            else:
                prompt_graph = build_minimax_h3_api_prompt(
                    prompt=req.prompt,
                    width=req.width,
                    height=req.height,
                    duration=req.duration,
                    seed=req.seed,
                    turbo_mode=req.turbo_mode,
                    steps=req.steps,
                    first_frame_filename=first_frame_name,
                    last_frame_filename=last_frame_name,
                    text_encoder=req.text_encoder,
                    filename_prefix=prefix,
                )

            # 4. Submit prompt to ComfyUI
            rec.status = "queued"
            rec.progress_message = "已提交至 ComfyUI 队列，等待执行..."
            self._save_tasks()

            comfy_base = f"http://127.0.0.1:{config.local_comfy_port}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{comfy_base}/prompt",
                    json={"prompt": prompt_graph, "client_id": rec.task_id},
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"ComfyUI 拒绝了工作流请求 ({resp.status_code}): {resp.text}")
                prompt_id = resp.json().get("prompt_id")
                rec.prompt_id = prompt_id
                rec.status = "running"
                rec.progress_message = f"正在使用 MiniMax H3 生成音视频 (prompt_id: {prompt_id})..."
                self._save_tasks()

            # 5. Poll ComfyUI /history/{prompt_id} until completed
            deadline = time.time() + 1800
            output_filenames: list[str] = []
            while time.time() < deadline:
                manager.touch_activity("api_task_polling")
                await asyncio.sleep(3.0)
                async with httpx.AsyncClient(timeout=10.0) as client:
                    h_resp = await client.get(f"{comfy_base}/history/{prompt_id}")
                    if h_resp.status_code == 200:
                        h_data = h_resp.json()
                        if prompt_id in h_data:
                            entry = h_data[prompt_id]
                            status_info = entry.get("status", {})
                            if status_info.get("status_str") == "error":
                                msgs = status_info.get("messages", [])
                                raise RuntimeError(f"ComfyUI 执行出错: {msgs}")

                            outputs = entry.get("outputs", {})
                            for node_id, node_out in outputs.items():
                                for key in ("videos", "images", "gifs", "audio"):
                                    for item in node_out.get(key, []):
                                        fn = item.get("filename")
                                        sub = item.get("subfolder", "")
                                        if fn:
                                            rel_path = f"{sub}/{fn}".lstrip("/")
                                            output_filenames.append(rel_path)
                            break

            # Flush & sync outputs directly to Google Drive (no local VPS storage)
            await manager.sync_outputs_from_remote()

            rec.completed_at = time.time()
            rec.output_files = output_filenames
            if output_filenames:
                rec.video_url = f"/api/v1/outputs/{output_filenames[0]}"
            rec.status = "completed"
            rec.progress_message = f"音视频生成完成并已保存至 Google Drive ({config.drive_base_dir}/outputs)"
            self._save_tasks()

        except Exception as e:
            rec.completed_at = time.time()
            rec.status = "failed"
            rec.error = str(e)
            rec.progress_message = f"生成失败: {e}"
            self._save_tasks()
        finally:
            manager.active_api_tasks = max(0, manager.active_api_tasks - 1)
            manager.touch_activity("api_task_end")


task_service = TaskExecutionService()
