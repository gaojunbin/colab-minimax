import random
import math
from typing import Optional
from app.config import config


def duration_to_frames(duration_sec: float, fps: int = 24) -> int:
    """Snaps duration in seconds to MiniMax H3's valid 17k+5 frame grid at 24fps."""
    raw = max(5, round(duration_sec * fps))
    frames = raw + (5 - (raw % 17)) % 17
    return int(frames)


def align_32(val: int) -> int:
    """Rounds resolution to nearest multiple of 32."""
    return max(256, int(round(val / 32) * 32))


def build_minimax_h3_api_prompt(
    prompt: str,
    width: int = 1344,
    height: int = 768,
    duration: float = 5.0,
    seed: Optional[int] = None,
    turbo_mode: bool = True,
    steps: Optional[int] = None,
    first_frame_filename: Optional[str] = None,
    last_frame_filename: Optional[str] = None,
    text_encoder: Optional[str] = None,
    filename_prefix: str = "video/MiniMax_H3",
) -> dict:
    """Builds a ComfyUI API prompt dictionary for MiniMax H3 (T2V / I2V / FL2V)."""
    if seed is None or seed < 0:
        seed = random.randint(1, 2**50 - 1)

    w = align_32(width)
    h = align_32(height)
    frames = duration_to_frames(duration, fps=24)

    if steps is None:
        actual_steps = 8 if turbo_mode else 20
    else:
        actual_steps = max(1, int(steps))

    lora_name = (
        config.turbo_lora_4step if actual_steps <= 4 else config.turbo_lora_8step
    )

    if text_encoder in ("heretic", "uncensored"):
        chosen_clip = config.heretic_clip_model
    elif text_encoder in ("official", "default", "awq"):
        chosen_clip = config.official_clip_model
    elif text_encoder and text_encoder.endswith(".safetensors"):
        chosen_clip = text_encoder
    else:
        chosen_clip = config.clip_model

    nodes: dict = {
        "119": {
            "class_type": "VAELoader",
            "inputs": {
                "vae_name": config.video_vae_model,
            },
        },
        "120": {
            "class_type": "VAELoader",
            "inputs": {
                "vae_name": config.audio_vae_model,
            },
        },
        "127": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": config.unet_model,
                "weight_dtype": "default",
            },
        },
        "128": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": chosen_clip,
                "type": "minimax",
                "device": "default",
            },
        },
        "129": {
            "class_type": "RandomNoise",
            "inputs": {
                "noise_seed": int(seed),
            },
        },
        "123": {
            "class_type": "KSamplerSelect",
            "inputs": {
                "sampler_name": "res_multistep",
            },
        },
    }

    # Model source (direct UNET or LoRA Turbo)
    model_ref = ["127", 0]
    if turbo_mode:
        nodes["134"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["127", 0],
                "lora_name": lora_name,
                "strength_model": 1.0,
            },
        }
        model_ref = ["134", 0]

    # Conditioning & Latent setup
    h3_inputs: dict = {
        "clip": ["128", 0],
        "vae": ["119", 0],
        "prompt": prompt,
        "width": w,
        "height": h,
        "length": frames,
    }

    if first_frame_filename:
        nodes["114"] = {
            "class_type": "LoadImage",
            "inputs": {
                "image": first_frame_filename,
            },
        }
        h3_inputs["first_frame"] = ["114", 0]

    if last_frame_filename:
        nodes["115"] = {
            "class_type": "LoadImage",
            "inputs": {
                "image": last_frame_filename,
            },
        }
        h3_inputs["last_frame"] = ["115", 0]

    nodes["131"] = {
        "class_type": "MiniMaxH3ImageToVideo",
        "inputs": h3_inputs,
    }

    nodes["124"] = {
        "class_type": "BasicScheduler",
        "inputs": {
            "model": model_ref,
            "scheduler": "simple",
            "steps": actual_steps,
            "denoise": 1.0,
        },
    }

    nodes["126"] = {
        "class_type": "BasicGuider",
        "inputs": {
            "model": model_ref,
            "conditioning": ["131", 0],
        },
    }

    nodes["125"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["129", 0],
            "guider": ["126", 0],
            "sampler": ["123", 0],
            "sigmas": ["124", 0],
            "latent_image": ["131", 1],
        },
    }

    nodes["122"] = {
        "class_type": "VAEDecode",
        "inputs": {
            "samples": ["125", 0],
            "vae": ["119", 0],
        },
    }

    nodes["121"] = {
        "class_type": "VAEDecodeAudio",
        "inputs": {
            "samples": ["125", 0],
            "vae": ["120", 0],
        },
    }

    nodes["130"] = {
        "class_type": "CreateVideo",
        "inputs": {
            "images": ["122", 0],
            "audio": ["121", 0],
            "fps": 24,
        },
    }

    nodes["92"] = {
        "class_type": "SaveVideo",
        "inputs": {
            "video": ["130", 0],
            "filename_prefix": filename_prefix,
            "format": "auto",
            "codec": "auto",
        },
    }

    return nodes
