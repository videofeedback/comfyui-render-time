# ComfyUI Render Time — config_manager.py
# Author: Ramiro Montes De Oca
# GitHub: https://github.com/videofeedback/comfyui-render-time
#
# Reads and writes plugin settings from/to config.json in the plugin directory.
# Author/contact identity is stored separately in author.txt (blank on fresh install).

import json
from pathlib import Path
from typing import Any

PLUGIN_DIR  = Path(__file__).parent
CONFIG_FILE = PLUGIN_DIR / "config.json"
AUTHOR_FILE = PLUGIN_DIR / "author.txt"

# Default configuration — all output types enabled, all default to ComfyUI output/.
# workflow_author / workflow_contact are intentionally NOT here — they live in author.txt
# so a fresh install always starts with blank identity fields.
DEFAULT_CONFIG: dict = {
    "embed_json": {
        "enabled": True,
        "location": "default",   # "default" | "custom"
        "custom_path": "",
    },
    "txt_report": {
        "enabled": True,
        "location": "default",
        "custom_path": "",
    },
    "isolated_json": {
        "enabled": True,
        "location": "default",
        "custom_path": "",
    },
    "workflow_png": {
        "enabled": True,
        "location": "default",
        "custom_path": "",
    },
    "workflow_mp4": {
        "location": "default",
        "custom_path": "",
    },
    "output_naming": {
        "title_mode": "default",  # "default" | "custom"
        "custom_title": "",
        "extra_mode": "default_t_ymd_hms_w",  # "default_t_ymd_hms_w" | "t_w" | "t" | "w" | "custom" | "none"
        "custom_extra": "",
    },
    # Embed workflow + render-time metadata into MP4 files written by Save Video
    "video_metadata_enabled": True,
    # Audio notification when a render + report completes
    "notify_on_complete": True,
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base recursively; base fills in missing keys."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def get_author_info() -> dict:
    """
    Read workflow_author / workflow_contact from author.txt.
    Returns empty strings for both fields if the file does not exist.
    This is intentional — a fresh install should never inherit someone else's name.
    """
    if not AUTHOR_FILE.exists():
        return {"workflow_author": "", "workflow_contact": ""}
    try:
        lines = AUTHOR_FILE.read_text(encoding="utf-8").splitlines()
        author  = lines[0].strip() if len(lines) > 0 else ""
        contact = lines[1].strip() if len(lines) > 1 else ""
        return {"workflow_author": author, "workflow_contact": contact}
    except Exception:
        return {"workflow_author": "", "workflow_contact": ""}


def save_author_info(author: str, contact: str) -> None:
    """
    Persist workflow_author / workflow_contact to author.txt.
    Called ONLY when the user explicitly saves from the Settings panel or
    Properties panel — never called automatically during report generation.
    """
    AUTHOR_FILE.write_text(f"{author}\n{contact}\n", encoding="utf-8")


def get_config() -> dict:
    """
    Load config from disk, filling in any missing keys with defaults.
    Author/contact are overlaid from author.txt (blank on fresh install).
    """
    if not CONFIG_FILE.exists():
        cfg = dict(DEFAULT_CONFIG)
    else:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                on_disk = json.load(f)
            # Strip any stale author fields that may have been saved by older versions
            on_disk.pop("workflow_author", None)
            on_disk.pop("workflow_contact", None)
            cfg = _deep_merge(DEFAULT_CONFIG, on_disk)
        except Exception:
            cfg = dict(DEFAULT_CONFIG)

    # Always overlay from author.txt so the frontend gets the right values
    cfg.update(get_author_info())
    return cfg


def save_config(cfg: dict) -> None:
    """
    Persist config to disk.  Author/contact fields are stripped — they belong
    in author.txt, not config.json.  Unknown keys are preserved as-is.
    """
    cfg_to_save = {k: v for k, v in cfg.items()
                   if k not in ("workflow_author", "workflow_contact")}
    merged = _deep_merge(DEFAULT_CONFIG, cfg_to_save)
    CONFIG_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")


def get_default_descriptions() -> dict:
    """Return human-readable default path descriptions for the UI."""
    return {
        "embed_json":    "Default: ComfyUI output/",
        "txt_report":    "Default: ComfyUI output/",
        "isolated_json": "Default: ComfyUI output/",
        "workflow_png":  "Default: ComfyUI output/",
        "workflow_mp4":  "Default: ComfyUI output/",
    }
