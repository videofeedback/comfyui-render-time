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
from urllib.parse import quote

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

def _get_machine_id() -> Optional[str]:
    """Return a unique machine identifier — cross-platform."""
    import sys as _sys
    # ── Windows ──────────────────────────────────────────────────────────────
    if _sys.platform == "win32":
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
    # ── macOS ─────────────────────────────────────────────────────────────────
    if _sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"') or None
        except Exception:
            return None
    # ── Linux ─────────────────────────────────────────────────────────────────
    try:
        return Path("/etc/machine-id").read_text(encoding="utf-8").strip() or None
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

    # Fall back to OS machine ID
    return _get_machine_id()


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

def _build_node_rows(record: dict, prompt_id: str, node_ids: Optional[set[str]] = None) -> list:
    """Return list of dicts, one per node, sorted by duration desc."""
    workflow = record.get("workflow")
    node_order = record.get("node_order", [])
    nodes_data = record.get("nodes", {})
    allowed = {str(n) for n in node_ids} if node_ids is not None else None

    # Build lookup: node_id → title from workflow
    title_map = {}
    if workflow:
        for n in workflow.get("nodes", []):
            nid = str(n.get("id", ""))
            meta = n.get("_meta") or {}
            title_map[nid] = meta.get("title") or n.get("type", "")

    rows = []
    for nid in node_order:
        if allowed is not None and str(nid) not in allowed:
            continue
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


def _iter_input_links(value):
    """Yield source node IDs from a ComfyUI API prompt input value."""
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        if isinstance(first, (str, int)):
            yield str(first)
        for item in value:
            yield from _iter_input_links(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_input_links(item)


def _upstream_node_ids(prompt: dict, node_id: str) -> set[str]:
    """Return node IDs needed to execute node_id, including node_id itself."""
    wanted: set[str] = set()

    def visit(nid: str) -> None:
        nid = str(nid)
        if nid in wanted:
            return
        wanted.add(nid)
        node_info = prompt.get(nid) or {}
        inputs = node_info.get("inputs") or {}
        for src_id in _iter_input_links(inputs):
            if src_id in prompt:
                visit(src_id)

    visit(str(node_id))
    return wanted


def _render_time_node_ids(record: dict) -> list[str]:
    """Return RenderTime node IDs in prompt order."""
    prompt = record.get("api_prompt") or {}
    ordered = []
    for nid, node_info in prompt.items():
        if isinstance(node_info, dict) and node_info.get("class_type") == "RenderTime":
            ordered.append(str(nid))
    return ordered


def _filter_media_for_nodes(items: list, node_ids: set[str]) -> list:
    """Keep media written by save nodes inside this RenderTime node's upstream scope."""
    result = []
    for item in items or []:
        item_node_id = item.get("node_id") if isinstance(item, dict) else None
        if item_node_id is not None and str(item_node_id) in node_ids:
            result.append(item)
    return result


def _scoped_record(record: dict, node_id: str) -> tuple[dict, set[str]]:
    """Return a shallow record copy limited to one RenderTime node's upstream graph."""
    prompt = record.get("api_prompt") or {}
    node_ids = _upstream_node_ids(prompt, str(node_id))
    scoped = dict(record)
    scoped["render_node_id"] = str(node_id)
    scoped["node_order"] = [
        nid for nid in record.get("node_order", [])
        if str(nid) in node_ids
    ]
    scoped["nodes"] = {
        nid: data for nid, data in (record.get("nodes") or {}).items()
        if str(nid) in node_ids
    }
    scoped["saved_images"] = _filter_media_for_nodes(record.get("saved_images") or [], node_ids)
    scoped["saved_videos"] = _filter_media_for_nodes(record.get("saved_videos") or [], node_ids)
    return scoped, node_ids


# ─── Workflow name extraction ─────────────────────────────────────────────────

def _workflow_name(record: dict, prompt_id: str) -> str:
    workflow = record.get("workflow")
    if workflow:
        extra = workflow.get("extra") or {}
        name = extra.get("workflow_name") or extra.get("name")
        if name:
            return str(name)
    return prompt_id[:8]


def _rows_total_sec(rows: list) -> float:
    """Return the scoped total duration represented by rows."""
    total = sum(
        float(r.get("duration_sec") or 0.0)
        for r in rows
        if not r.get("cached")
    )
    return round(total, 3)


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
    image_outputs = [
        v for v in (
            build_image_preview_info(image_info)
            for image_info in (record.get("saved_images") or [])
        )
        if v
    ]
    if image_outputs:
        entry["image_outputs"] = image_outputs
        entry["preview_image"] = image_outputs[-1]
    video_outputs = [
        v for v in (
            build_video_preview_info(video_info)
            for video_info in (record.get("saved_videos") or [])
        )
        if v
    ]
    if video_outputs:
        entry["video_outputs"] = video_outputs
        entry["preview_video"] = video_outputs[-1]
    return entry


def _build_output_preview_info(file_info, custom_route: str) -> Optional[dict]:
    """Return the compact preview payload used by the frontend."""
    if not isinstance(file_info, dict):
        return None

    filename = str(file_info.get("filename") or "").strip()
    if not filename:
        return None

    subfolder = str(file_info.get("subfolder") or "").strip()
    folder_type = str(file_info.get("folder_type") or "output").strip() or "output"
    fmt = str(file_info.get("format") or Path(filename).suffix.lstrip(".")).lower()

    preview = {
        "filename": filename,
        "subfolder": subfolder,
        "type": folder_type,
        "format": fmt,
    }

    path = file_info.get("path")
    if path:
        abs_path = str(path)
        preview["path"] = abs_path
        path_obj = Path(abs_path)
        try:
            output_dir = _get_output_dir()
            path_obj.parent.relative_to(output_dir)
            query = [
                f"filename={quote(filename, safe='')}",
                f"type={quote(folder_type, safe='')}",
            ]
            if subfolder:
                query.append(f"subfolder={quote(subfolder, safe='')}")
            preview["view_url"] = "/view?" + "&".join(query)
        except ValueError:
            preview["view_url"] = f"{custom_route}?path=" + quote(abs_path, safe="")
    else:
        query = [
            f"filename={quote(filename, safe='')}",
            f"type={quote(folder_type, safe='')}",
        ]
        if subfolder:
            query.append(f"subfolder={quote(subfolder, safe='')}")
        preview["view_url"] = "/view?" + "&".join(query)
    return preview


def build_image_preview_info(image_info) -> Optional[dict]:
    """Return the compact preview payload used by the frontend for image files."""
    return _build_output_preview_info(image_info, "/render-time/image-file")


def build_video_preview_info(video_info) -> Optional[dict]:
    """Return the compact preview payload used by the frontend for video files."""
    return _build_output_preview_info(video_info, "/render-time/video-file")


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


def build_managed_image_info(output_dir: Path, stem: str) -> dict:
    """Return the managed Render Time PNG descriptor for the workflow preview."""
    target_path = output_dir / build_output_filename(stem, "WORKFLOW", "png")

    output_root = _get_output_dir()
    subfolder = ""
    folder_type = "custom"
    try:
        parent_rel = target_path.parent.relative_to(output_root)
        subfolder = "" if str(parent_rel) == "." else parent_rel.as_posix()
        folder_type = "output"
    except ValueError:
        subfolder = ""

    return {
        "path": str(target_path),
        "filename": target_path.name,
        "subfolder": subfolder,
        "folder_type": folder_type,
        "format": "png",
    }


# ─── Timed workflow save ─────────────────────────────────────────────────────

def build_embedded_workflow(record: dict, timing_entry: dict) -> dict:
    """Return a workflow copy with the current timing entry embedded."""
    import copy

    workflow = record.get("workflow") or {}
    wf_copy = copy.deepcopy(workflow) if workflow else {}
    wf_copy.setdefault("extra", {})["render_time_report"] = [timing_entry]
    return wf_copy


def _build_timed_wf_content(record: dict, timing_entry: dict) -> str:
    """
    Return the JSON string of a workflow with timing_entry stored in
    extra.render_time_report[].
    """
    return json.dumps(build_embedded_workflow(record, timing_entry), indent=2, ensure_ascii=False)


def _write_timed_wf(directory: Path, filename: str, content: str, label: str) -> str:
    """Write content to directory/filename and return the full path string."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    print(f"[render-time] Workflow -> {label}: {path.name}")
    return str(path)


def _sanitize_filename_part(value: Optional[str]) -> str:
    """Return a Windows-safe filename fragment while preserving readability."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().endswith(".json"):
        text = text[:-5].strip()
    invalid = '<>:"/\\|?*'
    text = "".join("-" if ch in invalid or ord(ch) < 32 else ch for ch in text)
    text = " ".join(text.split())
    return text.strip(" .-_")


def _clean_title_candidate(value: Optional[str], workflow_token: str) -> str:
    """Filter out empty or generic title candidates."""
    text = _sanitize_filename_part(value)
    if not text:
        return ""
    lower = text.lower()
    if lower in {"workflow", "comfyui"}:
        return ""
    if workflow_token and lower == workflow_token.lower():
        return ""
    return text


def _resolve_original_output_title(
    prompt_id: str,
    record: dict,
    workflow_name: Optional[str] = None,
) -> str:
    """Return the original workflow title, if one can be discovered."""
    workflow_token = _sanitize_filename_part(prompt_id[:8]) or "workflow"
    workflow = record.get("workflow") or {}
    extra = workflow.get("extra") or {}
    matched_filename = _find_workflow_filename(workflow)

    candidates = [
        extra.get("workflow_name"),
        extra.get("name"),
        workflow.get("name"),
        workflow_name,
        matched_filename,
    ]
    for candidate in candidates:
        title = _clean_title_candidate(candidate, workflow_token)
        if title:
            return title
    return ""


def _render_time_node_prefix(record: dict, node_id: Optional[str] = None) -> str:
    """Return the first non-empty prefix configured on a RenderTime node."""
    prompt = record.get("api_prompt") or {}
    workflow = record.get("workflow") or {}
    target_id = str(node_id or record.get("render_node_id") or "") or None

    for nid, node_info in prompt.items():
        if not isinstance(node_info, dict):
            continue
        if node_info.get("class_type") != "RenderTime":
            continue
        if target_id is not None and str(nid) != target_id:
            continue
        inputs = node_info.get("inputs") or {}
        prefix = _sanitize_filename_part(inputs.get("prefix"))
        if prefix:
            return prefix

    try:
        for node in workflow.get("nodes", []):
            if node.get("type") != "RenderTime":
                continue
            if target_id is not None and str(node.get("id", "")) != target_id:
                continue
            widgets = node.get("widgets_values") or []
            prefix = _sanitize_filename_part(widgets[0] if widgets else "")
            if prefix:
                return prefix
    except Exception:
        pass

    return ""


def _build_timed_output_parts(
    prompt_id: str,
    record: dict,
    workflow_name: Optional[str] = None,
    cfg: Optional[dict] = None,
) -> list[str]:
    """Return the configured filename stem parts for this render."""
    cfg = cfg or config_manager.get_config()
    naming = cfg.get("output_naming") or {}

    wall_start = record.get("wall_start", datetime.datetime.now().timestamp())
    dt = datetime.datetime.fromtimestamp(wall_start)
    timestamp_token = dt.strftime("%Y%m%d-%H%M%S")
    workflow_token = _sanitize_filename_part(prompt_id[:8]) or "workflow"
    original_title = _resolve_original_output_title(prompt_id, record, workflow_name)

    title_mode = str(naming.get("title_mode") or "default").strip().lower()
    extra_mode = str(naming.get("extra_mode") or "default_t_ymd_hms_w").strip().lower()

    custom_title = _sanitize_filename_part(naming.get("custom_title"))
    custom_extra = _sanitize_filename_part(naming.get("custom_extra"))
    prefix_token = _render_time_node_prefix(record)

    title_token = original_title
    if title_mode == "custom" and custom_title:
        title_token = custom_title

    if extra_mode == "t_w":
        return [p for p in (prefix_token, title_token, workflow_token) if p]
    if extra_mode == "t":
        return [p for p in (prefix_token, title_token) if p] or [workflow_token]
    if extra_mode == "w":
        return [p for p in (prefix_token, workflow_token) if p]
    if extra_mode == "custom":
        base = custom_extra if custom_extra else (title_token if title_token else workflow_token)
        return [p for p in (prefix_token, base) if p]
    if extra_mode == "none":
        return [prefix_token] if prefix_token else []

    combined_suffix = f"{timestamp_token}-{workflow_token}"
    return [p for p in (prefix_token, title_token, combined_suffix) if p]


def get_timed_output_stem(
    prompt_id: str,
    record: dict,
    workflow_name: Optional[str] = None,
    cfg: Optional[dict] = None,
) -> str:
    """Return the shared Render Time filename stem for this run."""
    return "-".join(_build_timed_output_parts(prompt_id, record, workflow_name, cfg))


def build_output_filename(stem: str, output_label: str, extension: str) -> str:
    """Build the final filename for one Render Time output type."""
    label = _sanitize_filename_part(output_label) or "output"
    ext = str(extension or "").strip().lstrip(".")
    base = f"{stem}-{label}" if stem else label
    return f"{base}.{ext}" if ext else base


def save_timed_workflow(
    prompt_id: str,
    workflow_name: str,
    timing_entry: dict,
    record_override: Optional[dict] = None,
) -> dict:
    """
    Save all output files (workflow JSON, TXT, isolated JSON) according to current config.

    The shared output filename stem is built from the current Output File Naming
    settings. The timestamp portion, when enabled, is taken from the run's
    wall-clock start time.
    """
    record = record_override or timing_store.get_record(prompt_id)
    if record is None:
        return {"error": f"prompt_id {prompt_id} not found in store"}

    cfg = config_manager.get_config()
    output_dir = _get_output_dir()
    saved_paths = []
    stem = get_timed_output_stem(prompt_id, record, workflow_name, cfg)

    rows = _build_node_rows(record, prompt_id)
    total_sec = _rows_total_sec(rows)
    if cfg.get("txt_report", {}).get("enabled", True):
        txt_dir = _resolve_path(cfg.get("txt_report", {}), output_dir)
        txt_path = txt_dir / build_output_filename(stem, "LOG", "txt")
        author_info = config_manager.get_author_info()
        txt_content = _render_txt(
            prompt_id,
            record,
            rows,
            total_sec,
            author_info["workflow_author"],
            author_info["workflow_contact"],
        )
        saved_paths.append(_write_timed_wf(txt_dir, txt_path.name, txt_content, "LOG text report"))
        if record.get("render_node_id") is not None:
            timing_store.set_render_node_log_path(
                prompt_id,
                str(record.get("render_node_id")),
                str(txt_path),
            )
        else:
            timing_store.set_live_log_path(prompt_id, str(txt_path))

    # 1 ── Embedded report in JSON Workflow
    #      Full workflow JSON with render_time_report embedded in extra.
    if cfg["embed_json"]["enabled"]:
        filename_wf = build_output_filename(stem, "COMFYUI", "json")
        content_wf  = _build_timed_wf_content(record, timing_entry)
        out_dir = _resolve_path(cfg["embed_json"], output_dir)
        saved_paths.append(_write_timed_wf(out_dir, filename_wf, content_wf,
                                           "JSON Workflow"))

    # 2 ── Isolated JSON Render Time Report
    #      Pure timing JSON (NOT a workflow file). Contains plugin info, machine config,
    #      launch flags, total_sec, and all per-node data. 100% valid JSON, no ComfyUI
    #      graph structure.
    if cfg["isolated_json"]["enabled"]:
        filename_iso = build_output_filename(stem, "LOG", "json")
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

            png_dir = _resolve_path(cfg.get("workflow_png", {}), output_dir)
            png_info = build_managed_image_info(png_dir, stem)
            png_path = Path(str(png_info["path"]))
            filename_png = png_path.name
            png_path.parent.mkdir(parents=True, exist_ok=True)

            source_image_info = next(
                (
                    image_info
                    for image_info in reversed(record.get("saved_images") or [])
                    if Path(str(image_info.get("path") or "")).suffix.lower() == ".png"
                    and Path(str(image_info.get("path") or "")).exists()
                ),
                None,
            )

            if source_image_info:
                with PILImage.open(str(source_image_info["path"])) as src:
                    img = src.copy()
            else:
                try:
                    with PILImage.open(str(_DEFAULT_PNG)) as src:
                        img = src.convert("RGB")
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
            _wf_for_png = build_embedded_workflow(record, timing_entry)
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

            img.save(str(png_path), pnginfo=metadata, compress_level=4)
            print(f"[render-time] Workflow PNG -> output: {filename_png}")
            if record_override is None:
                timing_store.set_saved_images(prompt_id, [png_info])
            saved_paths.append(str(png_path))
        except Exception as exc:
            import traceback
            print(f"[render-time] Warning: could not save workflow PNG: {exc}")
            traceback.print_exc()

    return {"saved": saved_paths, "filename": stem}


# ─── Main entry point ────────────────────────────────────────────────────────

def _generate_prompt_wide_legacy(prompt_id: str) -> Optional[dict]:
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


def generate(prompt_id: str) -> Optional[dict]:
    """
    Generate independent reports for each RenderTime node in the prompt.
    Each report is scoped to the RenderTime node's upstream dependency graph.
    """
    record = timing_store.get_record(prompt_id)
    if record is None:
        return None

    reports_dir = _get_reports_dir()
    author_info = config_manager.get_author_info()
    workflow_author = author_info["workflow_author"]
    workflow_contact = author_info["workflow_contact"]
    render_node_ids = _render_time_node_ids(record) or [""]
    entries = []

    for render_node_id in render_node_ids:
        scoped_record, _node_ids = (
            _scoped_record(record, render_node_id)
            if render_node_id else (record, set(str(n) for n in record.get("node_order", [])))
        )
        rows = _build_node_rows(scoped_record, prompt_id)
        total_sec = _rows_total_sec(rows)
        md_suffix = f"_{render_node_id}" if render_node_id else ""
        md_stem = f"{datetime.date.today().strftime('%Y-%m-%d')}_{prompt_id[:8]}{md_suffix}"

        md_path = reports_dir / f"{md_stem}.md"
        md_content = _render_markdown(
            prompt_id,
            scoped_record,
            rows,
            total_sec,
            workflow_author,
            workflow_contact,
        )
        md_path.write_text(md_content, encoding="utf-8")
        print(f"[render-time] Report written: {md_path.name}")

        db_path = _get_metrics_db()
        if db_path:
            metrics_rec = _build_metrics_record(prompt_id, scoped_record, rows, total_sec)
            with open(db_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(metrics_rec) + "\n")

        timing_entry = build_timing_report_entry(prompt_id, scoped_record, rows, total_sec)
        if render_node_id:
            timing_entry["render_node_id"] = str(render_node_id)

        try:
            wf_name = (
                _find_workflow_filename(scoped_record.get("workflow"))
                or prompt_id[:8]
            )
            save_timed_workflow(
                prompt_id,
                wf_name,
                timing_entry,
                record_override=scoped_record,
            )
        except Exception as exc:
            print(f"[render-time] Warning: could not save timed workflow: {exc}")

        entries.append(timing_entry)

    if len(entries) == 1:
        return entries[0]
    return {"entries": entries, "latest": entries[-1]}
