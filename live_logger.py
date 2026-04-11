# ComfyUI Render Time — live_logger.py
# Author: Ramiro Montes De Oca
# GitHub: https://github.com/videofeedback/comfyui-render-time
#
# Writes a TXT log file in real-time during execution.
# Order: header → system info → node configs → live node lines → footer.
# The log is always closed BEFORE summary output files are written.

import datetime
import json as _json
from pathlib import Path
from typing import Optional

from .report_writer import (
    _MACHINE_CONFIG, _LAUNCH_FLAGS, _fmt_hms,
    _get_output_dir, _get_widget_names,
)

_SEP  = "=" * 72
_DASH = "-" * 40

# Active log runs keyed by prompt_id
_runs: dict = {}


class _LogRun:
    """Holds the open file handle and counters for one in-progress run."""

    def __init__(self, path: Path, fh, workflow: Optional[dict]):
        self.path         = path
        self.fh           = fh
        self.workflow     = workflow
        self.exec_count   = 0
        self.cached_count = 0
        self.error_node: Optional[str] = None
        self.error_msg:  Optional[str] = None

    def write(self, line: str = "") -> None:
        """Append one line and flush immediately (live output)."""
        try:
            self.fh.write(line + "\n")
            self.fh.flush()
        except Exception:
            pass

    def title_of(self, node_id: str, node_type: str) -> str:
        """Look up a node's display title from the stored workflow JSON."""
        try:
            for n in (self.workflow or {}).get("nodes", []):
                if str(n.get("id", "")) == str(node_id):
                    meta = n.get("_meta") or {}
                    return meta.get("title") or n.get("type", node_type)
        except Exception:
            pass
        return node_type


def _ts() -> str:
    """Current wall-clock time as HH:MM:SS.mmm"""
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:12]


# ─── Connection writer ───────────────────────────────────────────────────────

def _write_connections(run: "_LogRun", workflow: Optional[dict]) -> None:
    """
    Write a CONNECTIONS section: human-readable link table + two machine-
    readable JSON lines (LINKS_JSON / SLOTS_JSON) used by the reconstruct tool.
    """
    if not workflow:
        return

    links      = workflow.get("links") or []       # [[id,src,srcSlot,dst,dstSlot,type],...]
    wf_nodes   = workflow.get("nodes") or []

    if not links:
        return

    # Build node_id → node dict for label lookups
    nmap = {str(n.get("id", "")): n for n in wf_nodes}

    def _label(node_id, slot_idx, io):
        """Return 'NodeType.slot_name' or 'NodeType.slot_N'."""
        nd = nmap.get(str(node_id))
        if not nd:
            return f"?:{slot_idx}"
        ntype = nd.get("type", "?")
        key   = "inputs" if io == "i" else "outputs"
        slots = nd.get(key) or []
        if slot_idx < len(slots):
            sname = slots[slot_idx].get("name", f"slot_{slot_idx}")
        else:
            sname = f"slot_{slot_idx}"
        return f"{ntype}.{sname}"

    run.write("CONNECTIONS")
    run.write(_DASH)

    for lk in links:
        try:
            lid, src, ss, dst, ds, ltype = lk[0], lk[1], lk[2], lk[3], lk[4], lk[5]
            src_lbl = _label(src, ss, "o")
            dst_lbl = _label(dst, ds, "i")
            run.write(
                f" [{str(lid):>4}]  Node {str(src):>4}:{ss}  {src_lbl:<38}"
                f"  -->  Node {str(dst):>4}:{ds}  {dst_lbl}"
            )
        except Exception:
            pass

    run.write("")

    # ── Machine-readable blobs (single lines, parsed by reconstruct tool) ─────
    run.write("## MACHINE-READABLE — used by reconstruct tool ##")

    # LINKS_JSON : compact array of link tuples
    try:
        run.write("LINKS_JSON:" + _json.dumps(links, ensure_ascii=True, separators=(',', ':')))
    except Exception:
        pass

    # SLOTS_JSON : per-node input/output slot definitions
    slots_map = {}
    for nd in wf_nodes:
        nid = str(nd.get("id", ""))
        if not nid:
            continue
        raw_inputs  = nd.get("inputs")  or []
        raw_outputs = nd.get("outputs") or []

        def _slim_input(s):
            r = {"n": s.get("name",""), "t": s.get("type","")}
            lk = s.get("link")
            if lk is not None:
                r["lk"] = lk
            return r

        def _slim_output(s, idx):
            r = {"n": s.get("name",""), "t": s.get("type",""), "si": idx}
            lks = s.get("links") or []
            if lks:
                r["lks"] = lks
            return r

        slots_map[nid] = {
            "i": [_slim_input(s) for s in raw_inputs],
            "o": [_slim_output(s, i) for i, s in enumerate(raw_outputs)],
        }

    try:
        run.write("SLOTS_JSON:" + _json.dumps(slots_map, ensure_ascii=True, separators=(',', ':')))
    except Exception:
        pass

    run.write("")


# ─── Public API ──────────────────────────────────────────────────────────────

def open_log(
    prompt_id:  str,
    workflow:   Optional[dict],
    wall_start: float,
    wf_name:    str,
) -> None:
    """
    Create the live log file and write the static sections (header, system
    info, node configurations).  Called at prompt start, before any node runs.
    """
    try:
        dt      = datetime.datetime.fromtimestamp(wall_start)
        ts_file = dt.strftime("%Y%m%d-%H%M%S")
        base    = wf_name or prompt_id[:8]
        if base.lower().endswith(".json"):
            base = base[:-5]

        path = _get_output_dir() / f"{ts_file}-{base}-LOG.txt"
        fh   = open(str(path), "w", encoding="utf-8")
        run  = _LogRun(path, fh, workflow)
        _runs[prompt_id] = run

        cfg       = _MACHINE_CONFIG
        flags_str = " ".join(_LAUNCH_FLAGS) if _LAUNCH_FLAGS else "(none)"
        date_str  = dt.strftime("%Y-%m-%d  %H:%M:%S")

        # ── Header ───────────────────────────────────────────────────────────
        for line in [
            _SEP,
            " ComfyUI Render Time — Live Execution Log",
            f" Workflow  : {base}",
            f" Date      : {date_str}",
            f" Prompt ID : {prompt_id}",
            _SEP,
            "",
            "COMFYUI SYSTEM INFORMATION",
            _DASH,
            f" GPU       : {cfg['gpu_name']}",
            f" VRAM      : {cfg['vram_gb']} GB",
            f" RAM       : {cfg['ram_gb']} GB",
            f" OS        : {cfg['os']}",
            f" Python    : {cfg['python_version']}",
            f" PyTorch   : {cfg['pytorch_version']}",
            f" CUDA      : {cfg['cuda_version']}",
            f" ComfyUI   : {cfg['comfyui_version']}",
            f" Flags     : {flags_str}",
            "",
        ]:
            run.write(line)

        # ── Node Configuration ────────────────────────────────────────────────
        nodes_list = sorted(
            (workflow or {}).get("nodes", []),
            key=lambda n: int(n.get("id", 0)),
        )
        if nodes_list:
            run.write("NODE CONFIGURATION")
            run.write(_DASH)

            try:
                import nodes as _comfy_nodes
            except Exception:
                _comfy_nodes = None

            for n in nodes_list:
                nid   = n.get("id", "?")
                ntype = n.get("type", "unknown")
                meta  = n.get("_meta") or {}
                title = meta.get("title") or ntype
                wvals = n.get("widgets_values") or []

                run.write(f" [{str(nid):>3}]  {ntype:<28}  \"{title}\"")

                # Named widget values
                names: list = []
                if _comfy_nodes:
                    try:
                        cls = _comfy_nodes.NODE_CLASS_MAPPINGS.get(ntype)
                        if cls:
                            names = _get_widget_names(cls)
                    except Exception:
                        pass

                for i, val in enumerate(wvals):
                    key     = names[i] if i < len(names) else f"val_{i}"
                    val_str = str(val)
                    if len(val_str) > 64:
                        val_str = val_str[:61] + "…"
                    run.write(f"          {key:<22} = {val_str}")
                run.write("")

        # ── Connections ───────────────────────────────────────────────────────
        _write_connections(run, workflow)

        # ── Execution section header ──────────────────────────────────────────
        run.write("EXECUTION LOG")
        run.write(_DASH)

        print(f"[render-time] Live log → {path.name}")

    except Exception as exc:
        print(f"[render-time] Warning: could not open live log: {exc}")


def log_node_start(prompt_id: str, node_id: str, node_type: str) -> None:
    """Append a START line when a node begins executing."""
    run = _runs.get(prompt_id)
    if not run:
        return
    title = run.title_of(node_id, node_type)
    run.write(
        f" [{_ts()}]  START   Node {str(node_id):>4}  "
        f"{node_type:<28}  \"{title}\""
    )


def log_node_end(
    prompt_id: str, node_id: str, node_type: str, duration: float
) -> None:
    """Append an END line with elapsed time when a node finishes successfully."""
    run = _runs.get(prompt_id)
    if not run:
        return
    title = run.title_of(node_id, node_type)
    run.exec_count += 1
    run.write(
        f" [{_ts()}]  END     Node {str(node_id):>4}  "
        f"{node_type:<28}  \"{title}\"   {duration:.3f}s"
    )


def log_node_cached(prompt_id: str, node_id: str, node_type: str) -> None:
    """Append a CACHED line for nodes served from the ComfyUI cache."""
    run = _runs.get(prompt_id)
    if not run:
        return
    title = run.title_of(node_id, node_type)
    run.cached_count += 1
    run.write(
        f" [{_ts()}]  CACHED  Node {str(node_id):>4}  "
        f"{node_type:<28}  \"{title}\""
    )


def log_node_error(
    prompt_id: str, node_id: str, node_type: str, error: str
) -> None:
    """Append an ERROR block.  The run is NOT aborted — the caller re-raises."""
    run = _runs.get(prompt_id)
    if not run:
        return
    title = run.title_of(node_id, node_type)
    run.error_node = f"Node {node_id} — {node_type} (\"{title}\")"
    run.error_msg  = error
    run.write(
        f" [{_ts()}]  ERROR   Node {str(node_id):>4}  "
        f"{node_type:<28}  \"{title}\""
    )
    for line in error.splitlines():
        run.write(f"            !! {line}")


def close_log(prompt_id: str, total_sec: float) -> Optional[str]:
    """
    Write the summary footer and close the file.
    Returns the path string, or None on failure.
    Called BEFORE any other output files are generated.
    """
    run = _runs.pop(prompt_id, None)
    if not run:
        return None
    try:
        run.write("")
        run.write(_SEP)
        if run.error_msg:
            run.write(" STATUS     : FAILED")
            run.write(f" FAILED AT  : {run.error_node or 'unknown node'}")
            run.write(f" ERROR      : {run.error_msg[:160]}")
        else:
            run.write(" STATUS     : SUCCESS")
        run.write(f" TOTAL TIME : {total_sec:.3f}s  ({_fmt_hms(total_sec)})")
        run.write(
            f" EXECUTED   : {run.exec_count} node(s)   "
            f"{run.cached_count} cached"
        )
        run.write(_SEP)
        run.fh.close()
        print(f"[render-time] Live log closed  → {run.path.name}")
        return str(run.path)
    except Exception as exc:
        print(f"[render-time] Warning: could not close live log: {exc}")
        try:
            run.fh.close()
        except Exception:
            pass
        return None
