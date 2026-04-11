# ComfyUI Render Time — timing_store.py
# Author: Ramiro Montes De Oca
# GitHub: https://github.com/videofeedback/comfyui-render-time
#
# In-memory store for per-run, per-node timing data.

import time
from typing import Optional

# { prompt_id: { ... } }
_store: dict = {}
_latest_prompt_id: Optional[str] = None


# ─── Prompt lifecycle ────────────────────────────────────────────────────────

def prompt_start(prompt_id: str, api_prompt: dict, workflow: Optional[dict]) -> None:
    """Called at the start of a prompt execution."""
    _store[prompt_id] = {
        "api_prompt": api_prompt,
        "workflow": workflow,   # visual-editor JSON (None for headless API calls)
        "t_start": time.perf_counter(),
        "wall_start": time.time(),
        "nodes": {},
        "node_order": [],
        "total_sec": None,
        "completed": False,
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
