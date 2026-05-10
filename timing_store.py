# ComfyUI Render Time — timing_store.py
# Author: Ramiro Montes De Oca
# GitHub: https://github.com/videofeedback/comfyui-render-time
#
# In-memory store for per-run, per-node timing data.

import contextvars
import time
from typing import Optional

# { prompt_id: { ... } }
_store: dict = {}
_latest_prompt_id: Optional[str] = None
_active_prompt_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "render_time_active_prompt_id",
    default=None,
)
_active_prompt_fallback: Optional[str] = None


# ─── Prompt lifecycle ────────────────────────────────────────────────────────

def prompt_start(
    prompt_id: str,
    api_prompt: dict,
    workflow: Optional[dict],
    extra_pnginfo: Optional[dict] = None,
) -> None:
    """Called at the start of a prompt execution."""
    _store[prompt_id] = {
        "api_prompt": api_prompt,
        "workflow": workflow,   # visual-editor JSON (None for headless API calls)
        "extra_pnginfo": dict(extra_pnginfo or {}),
        "t_start": time.perf_counter(),
        "wall_start": time.time(),
        "nodes": {},
        "node_order": [],
        "total_sec": None,
        "completed": False,
        "saved_images": [],
        "saved_videos": [],
        "live_log_path": None,
    }


def prompt_end(prompt_id: str) -> None:
    """Called after a prompt execution finishes (success or failure)."""
    if prompt_id not in _store:
        return
    entry = _store[prompt_id]
    entry["total_sec"] = round(time.perf_counter() - entry["t_start"], 3)
    entry["completed"] = True
    global _latest_prompt_id
    _latest_prompt_id = prompt_id


# ─── Node lifecycle ──────────────────────────────────────────────────────────

def node_start(prompt_id: str, node_id: str, node_type: str) -> None:
    """Called just before a node's execute method is invoked."""
    if prompt_id not in _store:
        return
    _store[prompt_id]["nodes"][node_id] = {
        "node_type": node_type,
        "t_start": time.perf_counter(),
        "duration_sec": None,
        "cached": False,
    }
    if node_id not in _store[prompt_id]["node_order"]:
        _store[prompt_id]["node_order"].append(node_id)


def node_end(prompt_id: str, node_id: str) -> None:
    """Called just after a node's execute method returns."""
    if prompt_id not in _store:
        return
    nodes = _store[prompt_id]["nodes"]
    if node_id not in nodes:
        return
    entry = nodes[node_id]
    if entry.get("t_start") is not None:
        entry["duration_sec"] = round(time.perf_counter() - entry["t_start"], 3)


def node_cached(prompt_id: str, node_id: str, class_type: str) -> None:
    """Called when a node is served from cache (skipped execution)."""
    if prompt_id not in _store:
        return
    _store[prompt_id]["nodes"][node_id] = {
        "node_type": class_type,
        "duration_sec": 0.0,
        "cached": True,
    }
    if node_id not in _store[prompt_id]["node_order"]:
        _store[prompt_id]["node_order"].append(node_id)


# ─── Query helpers ───────────────────────────────────────────────────────────

def get_record(prompt_id: str) -> Optional[dict]:
    """Return the raw store entry for a prompt."""
    return _store.get(prompt_id)


def get_snapshot(prompt_id: str) -> Optional[dict]:
    """Return a serialisable timing snapshot (safe to JSON-dump)."""
    entry = _store.get(prompt_id)
    if entry is None:
        return None

    nodes_snap = {}
    for nid, ndata in entry["nodes"].items():
        nodes_snap[nid] = {
            "node_type": ndata["node_type"],
            "duration_sec": ndata.get("duration_sec"),
            "cached": ndata.get("cached", False),
        }

    return {
        "prompt_id": prompt_id,
        "total_sec": entry.get("total_sec"),
        "completed": entry.get("completed", False),
        "node_order": list(entry.get("node_order", [])),
        "nodes": nodes_snap,
    }


def get_node_duration(prompt_id: str, node_id: str) -> float:
    """Return the recorded duration_sec for a node, or 0.0 if not available."""
    try:
        return _store[prompt_id]["nodes"][node_id].get("duration_sec") or 0.0
    except (KeyError, TypeError):
        return 0.0


def get_latest_snapshot() -> Optional[dict]:
    """Return the snapshot for the most recent completed prompt."""
    if _latest_prompt_id is None:
        return None
    return get_snapshot(_latest_prompt_id)


def activate_prompt(prompt_id: str):
    """Mark a prompt as the currently executing prompt."""
    global _active_prompt_fallback
    _active_prompt_fallback = prompt_id
    return _active_prompt_id.set(prompt_id)


def reset_active_prompt(token) -> None:
    """Restore the previous active prompt context."""
    global _active_prompt_fallback
    try:
        _active_prompt_id.reset(token)
    finally:
        _active_prompt_fallback = _active_prompt_id.get()


def get_active_prompt_id() -> Optional[str]:
    """Return the prompt currently executing in this context."""
    return _active_prompt_id.get() or _active_prompt_fallback


def add_saved_video(prompt_id: str, video_info) -> None:
    """Record a video file written during the prompt."""
    if prompt_id not in _store or not video_info:
        return
    videos = _store[prompt_id].setdefault("saved_videos", [])
    if video_info not in videos:
        videos.append(video_info)


def get_saved_videos(prompt_id: str) -> list:
    """Return the list of saved video descriptors for a prompt."""
    entry = _store.get(prompt_id) or {}
    return list(entry.get("saved_videos", []))


def add_saved_image(prompt_id: str, image_info) -> None:
    """Record an image file written during the prompt."""
    if prompt_id not in _store or not image_info:
        return
    images = _store[prompt_id].setdefault("saved_images", [])
    if image_info not in images:
        images.append(image_info)


def get_saved_images(prompt_id: str) -> list:
    """Return the list of saved image descriptors for a prompt."""
    entry = _store.get(prompt_id) or {}
    return list(entry.get("saved_images", []))


def set_saved_images(prompt_id: str, image_infos: list) -> None:
    """Replace the saved image descriptors for a prompt."""
    if prompt_id not in _store:
        return
    _store[prompt_id]["saved_images"] = list(image_infos or [])


def set_saved_videos(prompt_id: str, video_infos: list) -> None:
    """Replace the saved video descriptors for a prompt."""
    if prompt_id not in _store:
        return
    _store[prompt_id]["saved_videos"] = list(video_infos or [])


def set_live_log_path(prompt_id: str, path: Optional[str]) -> None:
    """Record the final live-log path for a prompt."""
    if prompt_id not in _store:
        return
    _store[prompt_id]["live_log_path"] = path


def get_live_log_path(prompt_id: str) -> Optional[str]:
    """Return the final live-log path for a prompt."""
    entry = _store.get(prompt_id) or {}
    return entry.get("live_log_path")
