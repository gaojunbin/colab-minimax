import os
import json
from pathlib import Path
from pydantic import BaseModel


class AppConfig(BaseModel):
    port: int = int(os.getenv("PORT", "8188"))
    web_username: str = os.getenv("WEB_USERNAME", "admin")
    web_password: str = os.getenv("WEB_PASSWORD", "admin123")
    api_key: str = os.getenv("API_KEY", "sk-minimax-h3-colab-key")
    secret_key: str = os.getenv("SECRET_KEY", "minimax-h3-colab-super-secret-session-key-2026")

    colab_session_name: str = os.getenv("COLAB_SESSION_NAME", "minimax-h3")
    colab_auth_mode: str = os.getenv("COLAB_AUTH_MODE", "oauth2")
    default_gpu: str = os.getenv("DEFAULT_GPU", "L4")
    fallback_gpus: list[str] = [
        g.strip() for g in os.getenv("FALLBACK_GPUS", "L4,A100,T4").split(",") if g.strip()
    ]

    idle_timeout_seconds: int = int(os.getenv("IDLE_TIMEOUT_SECONDS", "1800"))
    auto_start_on_visit: bool = os.getenv("AUTO_START_ON_VISIT", "true").lower() in ("1", "true", "yes")

    enable_drive_mount: bool = os.getenv("ENABLE_DRIVE_MOUNT", "true").lower() in ("1", "true", "yes")
    drive_base_dir: str = os.getenv("DRIVE_BASE_DIR", "/content/drive/MyDrive/Colab-MiniMax-H3")
    install_comfyui_manager: bool = os.getenv("INSTALL_COMFYUI_MANAGER", "false").lower() in ("1", "true", "yes")
    prewarm_drive_models: bool = os.getenv("PREWARM_DRIVE_MODELS", "true").lower() in ("1", "true", "yes")

    hf_repo_id: str = os.getenv("HF_REPO_ID", "Comfy-Org/MiniMax-H3")
    heretic_repo_id: str = os.getenv(
        "HERETIC_REPO_ID", "Momoking/Qwen3-VL-32B-Heretic-MiniMax-H3-NVFP4"
    )
    unet_model: str = os.getenv("UNET_MODEL", "minimax_h3_fl2va_pruned_int8_convrot.safetensors")
    clip_model: str = os.getenv("CLIP_MODEL", "qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors")
    official_clip_model: str = os.getenv(
        "OFFICIAL_CLIP_MODEL", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
    )
    heretic_clip_model: str = os.getenv(
        "HERETIC_CLIP_MODEL", "qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors"
    )
    video_vae_model: str = os.getenv("VIDEO_VAE_MODEL", "minimax_h3_video_vae_int8_convrot.safetensors")
    audio_vae_model: str = os.getenv("AUDIO_VAE_MODEL", "minimax_h3_audio_vae_fp32.safetensors")
    turbo_lora_8step: str = os.getenv(
        "TURBO_LORA_8STEP", "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
    )
    turbo_lora_4step: str = os.getenv(
        "TURBO_LORA_4STEP", "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
    )

    data_dir: Path = Path(os.getenv("DATA_DIR", "/app/data"))
    local_comfy_port: int = int(os.getenv("LOCAL_COMFY_PORT", "18188"))


config = AppConfig()

SETTINGS_FILE = config.data_dir / "runtime_settings.json"


def load_runtime_settings() -> None:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if "default_gpu" in data:
                config.default_gpu = data["default_gpu"]
            if "idle_timeout_seconds" in data:
                config.idle_timeout_seconds = int(data["idle_timeout_seconds"])
            if "enable_drive_mount" in data:
                config.enable_drive_mount = bool(data["enable_drive_mount"])
            if "auto_start_on_visit" in data:
                config.auto_start_on_visit = bool(data["auto_start_on_visit"])
        except Exception:
            pass


def save_runtime_settings(updates: dict) -> dict:
    if "default_gpu" in updates and updates["default_gpu"] in ("L4", "A100", "H100", "T4", "G4"):
        config.default_gpu = updates["default_gpu"]
    if "idle_timeout_seconds" in updates:
        config.idle_timeout_seconds = max(60, int(updates["idle_timeout_seconds"]))
    if "enable_drive_mount" in updates:
        config.enable_drive_mount = bool(updates["enable_drive_mount"])
    if "auto_start_on_visit" in updates:
        config.auto_start_on_visit = bool(updates["auto_start_on_visit"])

    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "default_gpu": config.default_gpu,
        "idle_timeout_seconds": config.idle_timeout_seconds,
        "enable_drive_mount": config.enable_drive_mount,
        "auto_start_on_visit": config.auto_start_on_visit,
    }
    SETTINGS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
