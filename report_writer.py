# ComfyUI Render Time — report_writer.py
# Author: Ramiro Montes De Oca
# GitHub: https://github.com/videofeedback/comfyui-render-time
#
# Generates .md and .txt timing reports and appends records to run_metrics.jsonl.

import json
import os
import sys
import platform
import datetime
from pathlib import Path
from typing import Optional

from . import timing_store
from . import config_manager

# ─── Path resolution ─────────────────────────────────────────────────────────

def _find_rd_dir() -> Optional[Path]:
    """Locate the comfyui_rd analysis directory."""
    env = os.environ.get("COMFYUI_RD_DIR")
    if env:
        p = Path(env)
        if p.exists():
            return p

    try:
        import folder_paths
        comfyui_root = Path(folder_paths.base_path)
    except Exception:
        comfyui_root = Path(__file__).parent.parent.parent

    candidates = [
        comfyui_root / "comfyui_rd",
        comfyui_root.parent / "comfyui_rd",
        Path.home() / "Documents" / "claude-projects" / "comfyui_rd",
    ]
    for c in candidates:
        if c.exists() and (c / "db").exists():
            return c
    return None


def _get_reports_dir() -> Path:
    rd = _find_rd_dir()
    if rd:
        p = rd / "reports"
    else:
        try:
            import folder_paths
            p = Path(folder_paths.base_path) / "output" / "render_time_reports"
        except Exception:
            p = Path(__file__).parent / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_metrics_db() -> Optional[Path]:
    rd = _find_rd_dir()
    if rd:
        return rd / "db" / "run_metrics.jsonl"
    return None


# ─── Machine info (collected once at import) ─────────────────────────────────

def _collect_machine_config() -> dict:
    cfg = {
        "os": platform.system() + " " + platform.release(),
        "python_version": platform.python_version(),
        "pytorch_version": "unknown",
        "cuda_version": "n/a",
        "gpu_name": "unknown",
        "vram_gb": 0,
        "ram_gb": 0,
        "comfyui_version": "unknown",
    }
    try:
        import torch
        cfg["pytorch_version"] = torch.__version__
        if torch.cuda.is_available():
            cfg["cuda_version"] = torch.version.cuda or "n/a"
            cfg["gpu_name"] = torch.cuda.get_device_name(0)
            cfg["vram_gb"] = round(
                torch.cuda.get_device_properties(0).total_memory / (1024 ** 3), 1
            )
    except Exception:
        pass
    try:
        import psutil
        cfg["ram_gb"] = round(psutil.virtual_memory().total / (1024 ** 3))
    except Exception:
        pass
    try:
        import comfyui_version as _cv
        cfg["comfyui_version"] = getattr(_cv, "__version__", "unknown")
    except Exception:
        pass
    return cfg


PLUGIN_VERSION = "1.0.0"


def _fmt_hms(sec: float) -> str:
    """Format seconds as Xh XXm XXs.  Examples: 45.3→'45s'  90→'1m 30s'  3723→'1h 02m 03s'"""
    total = int(sec)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"

# Capture once at module load so it's available synchronously later
_MACHINE_CONFIG = _collect_machine_config()
_LAUNCH_FLAGS = sys.argv[1:]  # everything after main.py


# ─── Hardware context matching ───────────────────────────────────────────────

def _get_windows_machine_id() -> Optional[str]:
    """Read the permanent Machine GUID that Windows assigns to this installation."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        )
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        return str(value).strip() or None
    except Exception:
        return None


def _match_hardware_id() -> Optional[str]:
    """Return a hardware context ID from the local DB, or the Windows Machine GUID."""
    rd = _find_rd_dir()
    if rd:
        hw_db = rd / "db" / "hardware.jsonl"
        if hw_db.exists():
            gpu_name = _MACHINE_CONFIG.get("gpu_name", "").lower()
            try:
                with open(hw_db) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        rec_gpu = rec.get("gpu_name", "").lower()
                        if rec_gpu and rec_gpu in gpu_name:
                            hw_ctx = rec.get("hardware_context_id") or None
                            if hw_ctx:
                                return hw_ctx
            except Exception:
                pass

    # Fall back to the Windows Machine GUID
    return _get_windows_machine_id()


# ─── Widget name extraction ──────────────────────────────────────────────────

_WIDGET_PRIMITIVE_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN"}


def _get_widget_names(class_def) -> list:
    """Return ordered list of widget input names for a node class."""
    try:
        input_types = class_def.INPUT_TYPES()
    except Exception:
        return []
    names = []
    for category in ("required", "optional"):
        for name, type_info in input_types.get(category, {}).items():
            if name in ("hidden",):
                continue
            t = type_info[0] if isinstance(type_info, (list, tuple)) and type_info else type_info
            if isinstance(t, list):      # choices list → widget
                names.append(name)
            elif isinstance(t, str) and t in _WIDGET_PRIMITIVE_TYPES:
                names.append(name)
    return names


def _extract_node_settings(node_id: str, node_type: str, workflow: Optional[dict]) -> dict:
    """Map widget_values from the visual-editor workflow to named settings."""
    settings = {}
    if workflow is None:
        return settings

    # The visual editor stores nodes as a list with integer IDs
    wf_nodes = workflow.get("nodes", [])
    wf_node = None
    for n in wf_nodes:
        if str(n.get("id", "")) == str(node_id):
            wf_node = n
            break
    if wf_node is None:
        return settings

    widget_values = wf_node.get("widgets_values", [])
    if not widget_values:
        return settings

    # Try to get named inputs from the class definition
    try:
        import nodes as comfy_nodes
        class_def = comfy_nodes.NODE_CLASS_MAPPINGS.get(node_type)
        if class_def:
            names = _get_widget_names(class_def)
            for i, val in enumerate(widget_values):
                key = names[i] if i < len(names) else f"val_{i}"
                settings[key] = val
            return settings
    except Exception:
        pass

    # Fallback: positional keys
    for i, val in enumerate(widget_values):
        settings[f"val_{i}"] = val
    return settings


# ─── Next ID helper ──────────────────────────────────────────────────────────

def _next_metrics_id(db_path: Path) -> str:
    today = datetime.date.today().strftime("%Y%m%d")
    max_seq = 0
    if db_path.exists():
        try:
            with open(db_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    for val in rec.values():
                        if isinstance(val, str) and val.startswith(f"RM-{today}-"):
                            try:
                                seq = int(val.split("-")[-1])
                                max_seq = max(max_seq, seq)
                            except ValueError:
                                pass
        except Exception:
            pass
    return f"RM-{today}-{max_seq + 1:03d}"


# ─── Node table building ─────────────────────────────────────────────────────

def _build_node_rows(record: dict, prompt_id: str) -> list:
    """Return list of dicts, one per node, sorted by duration desc."""
    workflow = record.get("workflow")
    node_order = record.get("node_order", [])
    nodes_data = record.get("nodes", {})

    # Build lookup: node_id → title from workflow
    title_map = {}
    if workflow:
        for n in workflow.get("nodes", []):
            nid = str(n.get("id", ""))
            meta = n.get("_meta") or {}
            title_map[nid] = meta.get("title") or n.get("type", "")

    rows = []
    for nid in node_order:
        nd = nodes_data.get(nid, {})
        node_type = nd.get("node_type", "unknown")
        title = title_map.get(str(nid), node_type)
        duration = nd.get("duration_sec") or 0.0
        cached = nd.get("cached", False)
        settings = _extract_node_settings(nid, node_type, workflow)
        rows.append({
            "node_id": nid,
            "node_type": node_type,
            "title": title,
            "duration_sec": duration,
            "cached": cached,
            "settings": settings,
        })

    # Sort by duration descending (cached nodes go last)
    rows.sort(key=lambda r: (r["cached"], -r["duration_sec"]))
    return rows


# ─── Workflow name extraction ─────────────────────────────────────────────────

def _workflow_name(record: dict, prompt_id: str) -> str:
    workflow = record.get("workflow")
    if workflow:
        extra = workflow.get("extra") or {}
        name = extra.get("workflow_name") or extra.get("name")
        if name:
            return str(name)
    return prompt_id[:8]


# ─── Markdown report ─────────────────────────────────────────────────────────

def _render_markdown(
    prompt_id: str,
    record: dict,
    rows: list,
    total_sec: float,
    workflow_author: str = "",
    workflow_contact: str = "",
) -> str:
    cfg = _MACHINE_CONFIG
    hw_id = _match_hardware_id()
    date_str = datetime.date.today().isoformat()
    wf_name = _workflow_name(record, prompt_id)
    executed = sum(1 for r in rows if not r["cached"])
    cached_count = sum(1 for r in rows if r["cached"])
    flags_str = " ".join(_LAUNCH_FLAGS) if _LAUNCH_FLAGS else "(none)"

    header_lines = [
        "# ComfyUI Execution Timing Report",
        "",
        f"**Plugin Version:** `{PLUGIN_VERSION}`  ",
        f"**Workflow:** `{wf_name}`  ",
        f"**Date:** {date_str}  ",
        f"**Prompt ID:** `{prompt_id}`",
    ]
    if workflow_author:
        header_lines.append(f"**Workflow Author:** {workflow_author}  ")
    if workflow_contact:
        header_lines.append(f"**Contact:** {workflow_contact}  ")

    lines = header_lines + [
        "",
        "---",
        "",
        "## Machine Configuration",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Hardware Context | {hw_id} |",
        f"| GPU | {cfg['gpu_name']} |",
        f"| VRAM | {cfg['vram_gb']} GB |",
        f"| RAM | {cfg['ram_gb']} GB |",
        f"| OS | {cfg['os']} |",
        f"| Python | {cfg['python_version']} |",
        f"| PyTorch | {cfg['pytorch_version']} |",
        f"| CUDA | {cfg['cuda_version']} |",
        f"| ComfyUI | {cfg['comfyui_version']} |",
        "",
        "## ComfyUI Launch Flags",
        "",
        f"```",
        flags_str,
        "```",
        "",
        "---",
        "",
        "## Execution Summary",
        "",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| Total Time | **{total_sec:.2f}s** ({_fmt_hms(total_sec)}) |",
        f"| Nodes Executed | {executed} |",
        f"| Nodes Cached | {cached_count} |",
        f"| Total Nodes | {len(rows)} |",
        "",
        "---",
        "",
        "## Per-Node Breakdown",
        "",
        "| Rank | Node ID | Type | Title | Time (s) | % Total | Cached |",
        "|------|---------|------|-------|----------|---------|--------|",
    ]

    executed_rows = [r for r in rows if not r["cached"]]
    cached_rows = [r for r in rows if r["cached"]]

    rank = 1
    for r in executed_rows:
        pct = (r["duration_sec"] / total_sec * 100) if total_sec > 0 else 0
        lines.append(
            f"| {rank} | {r['node_id']} | {r['node_type']} | {r['title']} "
            f"| {r['duration_sec']:.3f} | {pct:.1f}% | — |"
        )
        rank += 1
    for r in cached_rows:
        lines.append(
            f"| — | {r['node_id']} | {r['node_type']} | {r['title']} "
            f"| 0.000 | 0.0% | ✓ |"
        )

    lines += [
        "",
        "---",
        "",
        "## Node Settings",
        "",
    ]

    for r in rows:
        cached_label = " *(cached)*" if r["cached"] else ""
        lines.append(f"### Node {r['node_id']} — {r['node_type']} (\"{r['title']}\"){cached_label}")
        lines.append("")
        if r["settings"]:
            lines.append("| Setting | Value |")
            lines.append("|---------|-------|")
            for k, v in r["settings"].items():
                lines.append(f"| {k} | `{v}` |")
        else:
            lines.append("*(no widget settings)*")
        lines.append("")

    lines += [
        "---",
        "",
        "## Bottleneck Summary",
        "",
    ]

    if executed_rows:
        top = executed_rows[0]
        top_pct = (top["duration_sec"] / total_sec * 100) if total_sec > 0 else 0
        lines.append(
            f"- **Top node:** `{top['node_type']}` — Node {top['node_id']} "
            f"({top_pct:.1f}% of total, {top['duration_sec']:.3f}s)"
        )
    if cached_count:
        lines.append(f"- **Cached nodes:** {cached_count} (0.000s combined)")

    return "\n".join(lines) + "\n"


# ─── Plain-text report ────────────────────────────────────────────────────────

def _render_txt(
    prompt_id: str,
    record: dict,
    rows: list,
    total_sec: float,
    workflow_author: str = "",
    workflow_contact: str = "",
) -> str:
    cfg = _MACHINE_CONFIG
    hw_id = _match_hardware_id()
    date_str = datetime.date.today().isoformat()
    wf_name = _workflow_name(record, prompt_id)
    executed = sum(1 for r in rows if not r["cached"])
    cached_count = sum(1 for r in rows if r["cached"])
    flags_str = " ".join(_LAUNCH_FLAGS) if _LAUNCH_FLAGS else "(none)"

    SEP  = "=" * 60
    DASH = "-" * 60

    lines = [
        SEP,
        " ComfyUI Execution Timing Report",
        f" Plugin Version : {PLUGIN_VERSION}",
        SEP,
        f" Workflow  : {wf_name}",
        f" Date      : {date_str}",
        f" Prompt ID : {prompt_id}",
    ]
    if workflow_author:
        lines.append(f" Author    : {workflow_author}")
    if workflow_contact:
        lines.append(f" Contact   : {workflow_contact}")
    lines.append("")
    lines += [
        DASH,
        " MACHINE CONFIGURATION",
        DASH,
        f" Hardware  : {hw_id}",
        f" GPU       : {cfg['gpu_name']}",
        f" VRAM      : {cfg['vram_gb']} GB",
        f" RAM       : {cfg['ram_gb']} GB",
        f" OS        : {cfg['os']}",
        f" Python    : {cfg['python_version']}",
        f" PyTorch   : {cfg['pytorch_version']}",
        f" CUDA      : {cfg['cuda_version']}",
        f" ComfyUI   : {cfg['comfyui_version']}",
        "",
        f" Launch Flags: {flags_str}",
        "",
        DASH,
        " EXECUTION SUMMARY",
        DASH,
        f" Total Time     : {total_sec:.2f}s  ({_fmt_hms(total_sec)})",
        f" Nodes Executed : {executed}",
        f" Nodes Cached   : {cached_count}",
        f" Total Nodes    : {len(rows)}",
        "",
        DASH,
        " PER-NODE BREAKDOWN",
        DASH,
    ]

    header = f" {'Rank':>4}  {'ID':>6}  {'Type':<24}  {'Title':<20}  {'Time(s)':>8}  {'%Total':>7}  {'Cached'}"
    lines.append(header)
    lines.append(" " + "-" * 58)

    rank = 1
    for r in rows:
        if r["cached"]:
            rank_str = "  --"
            pct_str = "  0.0%"
            time_str = "   0.000"
            cached_str = "  YES"
        else:
            pct = (r["duration_sec"] / total_sec * 100) if total_sec > 0 else 0
            rank_str = f"{rank:>4}"
            pct_str = f"{pct:>6.1f}%"
            time_str = f"{r['duration_sec']:>8.3f}"
            cached_str = "   no"
            rank += 1
        lines.append(
            f" {rank_str}  {r['node_id']:>6}  {r['node_type']:<24}  "
            f"{r['title']:<20}  {time_str}  {pct_str}  {cached_str}"
        )

    lines += [
        "",
        DASH,
        " NODE SETTINGS",
        DASH,
    ]

    for r in rows:
        cached_label = " (cached)" if r["cached"] else ""
        lines.append(f" Node {r['node_id']} -- {r['node_type']} (\"{r['title']}\"){cached_label}")
        if r["settings"]:
            for k, v in r["settings"].items():
                lines.append(f"   {k:<20} : {v}")
        else:
            lines.append("   (no widget settings)")
        lines.append("")

    lines += [
        DASH,
        " BOTTLENECK SUMMARY",
        DASH,
    ]

    executed_rows = [r for r in rows if not r["cached"]]
    if executed_rows:
        top = executed_rows[0]
        top_pct = (top["duration_sec"] / total_sec * 100) if total_sec > 0 else 0
        lines.append(
            f" Top node : {top['node_type']} (Node {top['node_id']}) "
            f"-- {top_pct:.1f}% of total ({top['duration_sec']:.3f}s)"
        )
    if cached_count:
        lines.append(f" Cached   : {cached_count} nodes (0.000s combined)")

    lines.append(SEP)
    return "\n".join(lines) + "\n"


# ─── JSONL metrics record ────────────────────────────────────────────────────

def _build_metrics_record(prompt_id: str, record: dict, rows: list, total_sec: float) -> dict:
    cfg = _MACHINE_CONFIG
    hw_id = _match_hardware_id()

    db_path = _get_metrics_db()
    metrics_id = _next_metrics_id(db_path) if db_path else f"RM-{prompt_id[:8]}"

    node_timings = []
    for r in rows:
        node_timings.append({
            "node_id": r["node_id"],
            "node_type": r["node_type"],
            "title": r["title"],
            "duration_sec": r["duration_sec"],
            "cached": r["cached"],
            "settings": r["settings"],
        })

    return {
        "render_metrics_id": metrics_id,
        "prompt_id": prompt_id,
        "workflow_id": "",
        "hardware_context_id": hw_id,
        "date": datetime.date.today().isoformat(),
        "resolution": "",
        "frame_count": 0,
        "fps_output": 0.0,
        "total_render_time_sec": total_sec,
        "per_frame_time_sec": 0.0,
        "vram_peak_gb": 0.0,
        "gpu_utilization_pct": 0,
        "batch_size": 1,
        "completed": record.get("completed", True),
        "failure_mode": "",
        "notes": "",
        "machine_config": {
            "hardware_id": hw_id,
            "gpu": cfg["gpu_name"],
            "vram_gb": cfg["vram_gb"],
            "ram_gb": cfg["ram_gb"],
            "python_version": cfg["python_version"],
            "pytorch_version": cfg["pytorch_version"],
            "comfyui_version": cfg["comfyui_version"],
        },
        "launch_flags": _LAUNCH_FLAGS,
        "node_timings": node_timings,
    }


# ─── Workflow JSON embedding helper ─────────────────────────────────────────

def build_timing_report_entry(prompt_id: str, record: dict, rows: list, total_sec: float) -> dict:
    """Build the dict to embed in workflow JSON extra.render_time_report[]."""
    machine_cfg = _MACHINE_CONFIG
    hw_id = _match_hardware_id()

    # Read workflow author/contact from author.txt (blank on fresh install)
    _author_info     = config_manager.get_author_info()
    workflow_author  = _author_info["workflow_author"]
    workflow_contact = _author_info["workflow_contact"]

    node_order_list = record.get("node_order", [])
    nodes_compact = {}
    for r in rows:
        nid = r["node_id"]
        try:
            exec_idx = node_order_list.index(nid)
        except ValueError:
            exec_idx = 9999
        nodes_compact[nid] = {
            "type": r["node_type"],
            "title": r["title"],
            "duration_sec": r["duration_sec"],
            "cached": r["cached"],
            "settings": r["settings"],
            "exec_order": exec_idx,   # 0-based execution sequence
        }

    wall_start = record.get("wall_start")
    run_date = (
        datetime.datetime.fromtimestamp(wall_start).date().isoformat()
        if wall_start else datetime.date.today().isoformat()
    )

    entry = {
        "plugin": "ComfyUI Render Time",
        "plugin_version": PLUGIN_VERSION,
        "author": "Ramiro Montes De Oca",
        "github": "https://github.com/videofeedback/comfyui-render-time",
        "run_id": prompt_id,
        "date": run_date,
        "total_sec": total_sec,
        "machine_config": {
            "gpu": machine_cfg["gpu_name"],
            "vram_gb": machine_cfg["vram_gb"],
            "ram_gb": machine_cfg["ram_gb"],
            "python_version": machine_cfg["python_version"],
            "pytorch_version": machine_cfg["pytorch_version"],
            "comfyui_version": machine_cfg["comfyui_version"],
        },
        "launch_flags": _LAUNCH_FLAGS,
        "nodes": nodes_compact,
    }
    entry["hardware_id"] = hw_id
    if workflow_author:
        entry["workflow_author"] = workflow_author
    if workflow_contact:
        entry["workflow_contact"] = workflow_contact
    return entry


# ─── Workflow filename matcher ───────────────────────────────────────────────

def _find_workflow_filename(workflow: Optional[dict]) -> Optional[str]:
    """
    Scan user/default/workflows/*.json and return the stem (no extension) of the
    file whose {node_id: node_type} signature matches the current workflow.
    Returns None if no match is found.
    """
    if not workflow:
        return None

    # Build signature from current workflow
    current_sig = {
        str(n.get("id", "")): n.get("type", "")
        for n in workflow.get("nodes", [])
    }
    if not current_sig:
        return None

    try:
        import folder_paths
        base = Path(folder_paths.base_path)
    except Exception:
        base = Path(__file__).parent.parent.parent

    workflows_dir = base / "user" / "default" / "workflows"
    if not workflows_dir.exists():
        return None

    for wf_file in sorted(workflows_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with open(wf_file, encoding="utf-8") as f:
                wf = json.load(f)
            file_sig = {
                str(n.get("id", "")): n.get("type", "")
                for n in wf.get("nodes", [])
            }
            if file_sig == current_sig:
                return wf_file.stem   # e.g. "video_ltx2_3_i2v_-v2-local"
        except Exception:
            continue
    return None


# ─── Config-aware path resolver ──────────────────────────────────────────────

def _resolve_path(cfg_entry: dict, default_dir: Path) -> Path:
    """
    Return the directory to use for an output type.
    Uses custom_path when location == "custom" and the path is non-empty,
    otherwise falls back to default_dir.
    """
    if cfg_entry.get("location") == "custom":
        custom = cfg_entry.get("custom_path", "").strip()
        if custom:
            p = Path(custom)
            p.mkdir(parents=True, exist_ok=True)
            return p
    return default_dir


def _get_output_dir() -> Path:
    """Return the ComfyUI output directory."""
    try:
        import folder_paths
        p = Path(folder_paths.get_output_directory())
    except Exception:
        p = Path(__file__).parent.parent.parent / "output"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ─── Workflow PNG — plugin default image ─────────────────────────────────────

_DEFAULT_PNG = Path(__file__).parent / "default.png"


# ─── Timed workflow save ─────────────────────────────────────────────────────

def _build_timed_wf_content(record: dict, timing_entry: dict) -> str:
    """
    Return the JSON string of a workflow with timing_entry stored in
    extra.render_time_report[].
    """
    import copy
    workflow = record.get("workflow") or {}
    wf_copy = copy.deepcopy(workflow) if workflow else {}
    if "extra" not in wf_copy:
        wf_copy["extra"] = {}
    # Always replace — keep only the current run, never accumulate past entries
    wf_copy["extra"]["render_time_report"] = [timing_entry]
    return json.dumps(wf_copy, indent=2, ensure_ascii=False)


def _write_timed_wf(directory: Path, filename: str, content: str, label: str) -> str:
    """Write content to directory/filename and return the full path string."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    print(f"[render-time] Workflow → {label}: {path.name}")
    return str(path)


def save_timed_workflow(prompt_id: str, workflow_name: str, timing_entry: dict) -> dict:
    """
    Save all output files (workflow JSON, TXT, isolated JSON) according to current config.

    Base filename format: <workflow_name>--rendertime-DDMMYYYY-HH-MM-SS
    The timestamp is taken from the run's wall-clock start time.
    All three output types default to the ComfyUI output/ folder.

    Called directly by generate() and also via the HTTP route /render-time/save-workflow
    (JS fallback).  The config flags are honoured in both cases.
    """
    record = timing_store.get_record(prompt_id)
    if record is None:
        return {"error": f"prompt_id {prompt_id} not found in store"}

    # Timestamp from run start (wall clock)
    wall_start = record.get("wall_start", datetime.datetime.now().timestamp())
    dt = datetime.datetime.fromtimestamp(wall_start)
    ts = dt.strftime("%Y%m%d-%H%M%S")     # e.g. 20260410-143045

    # Strip .json extension if present
    base_name = workflow_name
    if base_name.lower().endswith(".json"):
        base_name = base_name[:-5]

    cfg = config_manager.get_config()
    output_dir = _get_output_dir()
    saved_paths = []

    # 1 ── Embedded report in JSON Workflow
    #      Full workflow JSON with render_time_report embedded in extra.
    if cfg["embed_json"]["enabled"]:
        filename_wf = f"{ts}-{base_name}-COMFYUI.json"
        content_wf  = _build_timed_wf_content(record, timing_entry)
        out_dir = _resolve_path(cfg["embed_json"], output_dir)
        saved_paths.append(_write_timed_wf(out_dir, filename_wf, content_wf,
                                           "JSON Workflow"))

    # 2 ── Isolated JSON Render Time Report
    #      Pure timing JSON (NOT a workflow file). Contains plugin info, machine config,
    #      launch flags, total_sec, and all per-node data. 100% valid JSON, no ComfyUI
    #      graph structure.
    if cfg["isolated_json"]["enabled"]:
        filename_iso = f"{ts}-{base_name}-LOG.json"
        iso_entry    = {k: v for k, v in timing_entry.items()
                        if k != "author"}
        iso_content  = json.dumps(iso_entry, indent=2, ensure_ascii=False)
        iso_dir = _resolve_path(cfg["isolated_json"], output_dir)
        saved_paths.append(_write_timed_wf(iso_dir, filename_iso, iso_content,
                                           "isolated JSON Render Time Report"))

    # 4 ── Embedded Workflow PNG
    #      Uses default.png from the plugin folder as the thumbnail image.
    #      Full workflow JSON embedded as "workflow" tEXt chunk — drag into ComfyUI
    #      to reload the workflow. Also embeds timing data.
    if cfg.get("workflow_png", {}).get("enabled", True):
        try:
            from PIL import Image as PILImage
            from PIL.PngImagePlugin import PngInfo

            filename_png = f"{ts}-{base_name}-WORKFLOW.png"
            png_dir = _resolve_path(cfg.get("workflow_png", {}), output_dir)
            png_dir.mkdir(parents=True, exist_ok=True)

            # Load plugin default thumbnail; fall back to small placeholder
            try:
                img = PILImage.open(str(_DEFAULT_PNG)).convert("RGB")
            except Exception:
                img = PILImage.new("RGB", (256, 144), color=(30, 30, 30))

            # Embed workflow JSON + timing data as tEXt metadata.
            # PNG tEXt chunks are Latin-1 — ensure_ascii=True escapes all non-ASCII
            # as \uXXXX so the output is pure ASCII and always safe for tEXt.
            # separators=(',',':') keeps the JSON compact to reduce chunk size.
            #
            # IMPORTANT: build a fresh copy of the workflow with render_time_report
            # replaced by the CURRENT run's timing — never embed stale data from a
            # previous session that was already sitting in workflow["extra"].
            import copy as _copy
            _wf_for_png = _copy.deepcopy(record.get("workflow") or {})
            _wf_for_png.setdefault("extra", {})["render_time_report"] = [timing_entry]
            wf_json = json.dumps(
                _wf_for_png,
                ensure_ascii=True, separators=(',', ':'),
            )
            tr_json = json.dumps(
                timing_entry,
                ensure_ascii=True, separators=(',', ':'),
            )
            metadata = PngInfo()
            metadata.add_text("workflow",      wf_json)
            metadata.add_text("timing_report", tr_json)

            img.save(str(png_dir / filename_png), pnginfo=metadata, compress_level=4)
            print(f"[render-time] Workflow PNG → output: {filename_png}")
            saved_paths.append(str(png_dir / filename_png))
        except Exception as exc:
            import traceback
            print(f"[render-time] Warning: could not save workflow PNG: {exc}")
            traceback.print_exc()

    return {"saved": saved_paths, "filename": f"{ts}-{base_name}"}


# ─── Main entry point ────────────────────────────────────────────────────────

def generate(prompt_id: str) -> Optional[dict]:
    """
    Generate timing reports for the given prompt_id.
    Returns the timing-report entry dict (for embedding in workflow JSON).
    """
    record = timing_store.get_record(prompt_id)
    if record is None:
        return None

    total_sec = record.get("total_sec") or 0.0
    rows = _build_node_rows(record, prompt_id)

    # Build file stem
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    stem = f"{date_str}_{prompt_id[:8]}"
    reports_dir = _get_reports_dir()

    cfg = config_manager.get_config()
    # Author/contact come from author.txt (blank on fresh install, never inherited from config.json)
    _author_info     = config_manager.get_author_info()
    workflow_author  = _author_info["workflow_author"]
    workflow_contact = _author_info["workflow_contact"]

    # Write Markdown (always — primary analysis artifact)
    md_path = reports_dir / f"{stem}.md"
    md_content = _render_markdown(prompt_id, record, rows, total_sec, workflow_author, workflow_contact)
    md_path.write_text(md_content, encoding="utf-8")
    print(f"[render-time] Report written: {md_path.name}")

    # Append to run_metrics.jsonl
    db_path = _get_metrics_db()
    if db_path:
        metrics_rec = _build_metrics_record(prompt_id, record, rows, total_sec)
        with open(db_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(metrics_rec) + "\n")

    # Build timing entry
    timing_entry = build_timing_report_entry(prompt_id, record, rows, total_sec)

    # Save timestamped workflow JSON directly from Python (no JS round-trip needed)
    try:
        wf_name = (
            _find_workflow_filename(record.get("workflow"))
            or prompt_id[:8]
        )
        save_timed_workflow(prompt_id, wf_name, timing_entry)
    except Exception as exc:
        print(f"[render-time] Warning: could not save timed workflow: {exc}")

    return timing_entry
