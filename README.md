# ComfyUI Render Time

Real-time per-node execution timing, live logging, and workflow reconstruction for [ComfyUI](https://github.com/comfyanonymous/ComfyUI).

Add the **Render Time** node to any workflow — no connections required. It automatically captures how long every node takes, which nodes were cached, and your full system configuration, then writes structured output files you can analyse or drag back into ComfyUI.

---

## Features

- **Per-node wall-clock timing** — measures every node that executes or is served from cache
- **Live execution log** — real-time `.txt` file written line-by-line as nodes run, not after
- **Sortable timing table** in the node UI with visual duration bars and click-to-highlight
- **Workflow reconstruction** — log files embed full connection data so any past run can be rebuilt as a loadable ComfyUI workflow
- **Multi-format output** — `LOG.txt`, `LOG.json`, `COMFYUI.json`, `WORKFLOW.png`
- **Hardware fingerprint** — GPU, VRAM, RAM, Python, PyTorch, CUDA, ComfyUI version captured at run time
- **Author identity** — optional workflow author and contact stored in `author.txt`, never hardcoded in config
- **Audio notification** — optional chime when a render completes
- **Per-tab data isolation** — correct timing shown when switching between workflow tabs
- **Click any row** in the timing table to centre that node in the ComfyUI canvas

---

## Installation

### Via ComfyUI Manager (recommended)
Search for **ComfyUI Render Time** in the built-in manager and click Install.

### Manual
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/videofeedback/comfyui-render-time comfyui-Render-Time
pip install -r comfyui-Render-Time/requirements.txt
```
Restart ComfyUI.

---

## Quick Start

1. Open any workflow in ComfyUI
2. Double-click the canvas → search **Render Time** → add the node
3. Queue a prompt — no connections needed
4. Watch the node fill with timing data when the run completes

The node updates automatically via WebSocket. Switch workflow tabs and each node shows its own run's data.

---

## The Node UI

The node has two tabs.

### ⏱ Timing Tab

| Column | Description |
|--------|-------------|
| `#` | Execution order (0-based) |
| `ID` | Node ID |
| `Type` | Node class name |
| `Title` | Display title from the canvas |
| `Time` | Wall-clock duration in seconds |
| `%` | Share of total render time (with bar) |

Click any column header to sort. Cached nodes are labelled **[CACHED]** and count as 0 s.

Below the table a collapsible **Node Settings** section lists every widget value for each node.

### ⚙ Settings Tab

| Setting | Description |
|---------|-------------|
| Author / Contact | Written to `author.txt` on save; embedded in output files |
| Embed JSON | Toggle `COMFYUI.json` output |
| Isolated JSON | Toggle `LOG.json` output |
| Workflow PNG | Toggle `WORKFLOW.png` output |
| Notify on complete | Audio chime at end of each run |

Each output type can be sent to the default `output/` folder or a custom path.

---

## Output Files

Every run produces up to four files. All filenames follow the format:

```
YYYYMMDD-HHMMSS-{workflow_name}-{TYPE}.{ext}
```

| File | Type | Description |
|------|------|-------------|
| `…-LOG.txt` | Plain text | **Live execution log** — written in real time during the run |
| `…-LOG.json` | JSON | Isolated timing data — plugin info, machine config, per-node durations |
| `…-COMFYUI.json` | JSON | Full workflow with timing embedded in `extra.render_time_report` — drag into ComfyUI to reload |
| `…-WORKFLOW.png` | PNG | Thumbnail image with workflow JSON and timing embedded as metadata — drag into ComfyUI to reload |

### LOG.txt structure

```
========================================================================
 ComfyUI Render Time — Live Execution Log
 Workflow  : my_workflow
 Date      : 2026-04-10  17:09:57
 Prompt ID : 38fa30f9-…
========================================================================

COMFYUI SYSTEM INFORMATION
----------------------------------------
 GPU       : NVIDIA GeForce RTX 4090
 VRAM      : 24.0 GB
 …

NODE CONFIGURATION
----------------------------------------
 [ 71]  KSamplerAdvanced            "KSamplerAdvanced"
          add_noise              = enable
          noise_seed             = 258965858433509
          …

CONNECTIONS
----------------------------------------
 [   1]  Node   72:0  UNETLoader.MODEL  -->  Node   71:0  KSamplerAdvanced.model
 …
## MACHINE-READABLE — used by reconstruct tool ##
LINKS_JSON:[…]
SLOTS_JSON:{…}

EXECUTION LOG
----------------------------------------
 [17:09:57.123]  START   Node   72  UNETLoader            "UNETLoader"
 [17:10:01.456]  END     Node   72  UNETLoader            "UNETLoader"   4.333s
 [17:10:01.457]  CACHED  Node   85  CLIPLoader            "CLIPLoader"
 …

========================================================================
 STATUS     : SUCCESS
 TOTAL TIME : 43.210s  (43s)
 EXECUTED   : 8 node(s)   3 cached
========================================================================
```

---

## Workflow Reconstruction

Any `-LOG.txt` file produced by this plugin can be reconstructed into a loadable ComfyUI workflow JSON — including all node connections.

Use the built-in reconstruct script:

```bash
python - << 'EOF'
import json, re
from pathlib import Path

log_path = Path(r"C:/ComfyUI/output/20260410-170957-my_workflow-LOG.txt")
out_path = log_path.parent / (log_path.stem.replace("-LOG", "-reconstructed") + ".json")

text = log_path.read_text(encoding="utf-8")

# Parse nodes
nodes = []
node_cfg = re.search(r"NODE CONFIGURATION\n-+\n(.*?)(?:CONNECTIONS|EXECUTION LOG)", text, re.DOTALL)
for block in re.split(r"(?=^ \[)", node_cfg.group(1), flags=re.MULTILINE):
    m = re.match(r"\[\s*(\d+)\]\s+(\S+)\s+\"([^\"]*)\"", block.strip().splitlines()[0].strip())
    if not m: continue
    widgets = []
    for line in block.strip().splitlines()[1:]:
        wm = re.match(r"\s+\S+\s*=\s*(.*)", line)
        if not wm: continue
        raw = wm.group(1).strip()
        try: widgets.append(int(raw)); continue
        except ValueError: pass
        try: widgets.append(float(raw)); continue
        except ValueError: pass
        widgets.append(raw)
    nodes.append({"id": int(m.group(1)), "type": m.group(2), "title": m.group(3), "widgets": widgets})

# Parse connections
links_json = slots_json = None
for line in text.splitlines():
    if line.startswith("LINKS_JSON:"): links_json = json.loads(line[len("LINKS_JSON:"):])
    elif line.startswith("SLOTS_JSON:"): slots_json = json.loads(line[len("SLOTS_JSON:"):])

# Build workflow
COLS, NW, NH, GX, GY = 5, 320, 160, 30, 30
wf_nodes = []
for idx, node in enumerate(nodes):
    nid = str(node["id"])
    inputs, outputs = [], []
    if slots_json and nid in slots_json:
        sd = slots_json[nid]
        for s in sd.get("i") or []:
            inp = {"name": s["n"], "type": s["t"]}
            if "lk" in s: inp["link"] = s["lk"]
            inputs.append(inp)
        for s in sd.get("o") or []:
            outputs.append({"name": s["n"], "type": s["t"], "links": s.get("lks") or [], "slot_index": s.get("si", 0)})
    wf_nodes.append({
        "id": node["id"], "type": node["type"],
        "pos": [(idx % COLS) * (NW + GX), (idx // COLS) * (NH + GY)],
        "size": {"0": NW, "1": NH}, "flags": {}, "order": idx, "mode": 0,
        "inputs": inputs, "outputs": outputs,
        "properties": {"Node name for S&R": node["type"]},
        "widgets_values": node["widgets"],
        **({"_meta": {"title": node["title"]}} if node["title"] != node["type"] else {}),
    })

workflow = {
    "last_node_id": max(n["id"] for n in nodes) + 1,
    "last_link_id": max((lk[0] for lk in (links_json or [])), default=0),
    "nodes": wf_nodes, "links": links_json or [],
    "groups": [], "config": {}, "version": 0.4,
    "extra": {"workflow_name": f"Reconstructed from {log_path.name}"},
}
out_path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Saved: {out_path.name}  ({len(wf_nodes)} nodes, {len(links_json or [])} links)")
EOF
```

Drag the resulting `-reconstructed.json` into ComfyUI to load the workflow.

> **Note:** Reconstruction requires a log produced by version 1.0.0 or later. Older logs do not contain the `LINKS_JSON` / `SLOTS_JSON` machine-readable blocks and will produce an unconnected skeleton.

---

## Configuration

Settings are stored in `config.json` inside the plugin folder. All can be changed from the ⚙ Settings tab in the node.

| Key | Default | Description |
|-----|---------|-------------|
| `embed_json.enabled` | `true` | Write `COMFYUI.json` |
| `embed_json.location` | `"default"` | `"default"` → ComfyUI `output/`, `"custom"` → `custom_path` |
| `isolated_json.enabled` | `true` | Write `LOG.json` |
| `isolated_json.location` | `"default"` | — |
| `workflow_png.enabled` | `true` | Write `WORKFLOW.png` |
| `workflow_png.location` | `"default"` | — |
| `notify_on_complete` | `true` | Browser audio chime on run completion |

### Author identity

Author name and contact are stored in `author.txt` (two lines: name, then contact). The file is created only when you click **Apply & Save** in the Settings tab. A fresh install always starts with blank fields — no author info is inherited from `config.json`.

---

## HTTP API

The plugin exposes a small REST API on the ComfyUI server.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/render-time/latest` | Timing snapshot for the most recent run |
| `GET` | `/render-time/latest/full` | Full timing entry (same shape as WebSocket event) |
| `GET` | `/render-time/{prompt_id}` | Timing snapshot for a specific run |
| `GET` | `/render-time/config` | Current plugin configuration |
| `POST` | `/render-time/config` | Save full configuration |
| `POST` | `/render-time/config/property` | Save a single configuration key |

---

## File Structure

```
comfyui-Render-Time/
├── __init__.py           ComfyUI entry point — patches execution engine, registers routes
├── timing_store.py       In-memory per-run timing data store
├── live_logger.py        Real-time LOG.txt writer
├── report_writer.py      Output file generator (JSON, PNG, machine config)
├── config_manager.py     config.json + author.txt read/write
├── default.png           Thumbnail used for WORKFLOW.png output
├── author.txt            Author/contact identity (created on first save)
├── config.json           Plugin settings (created on first save)
├── pyproject.toml        Comfy Registry metadata
├── requirements.txt      Python dependencies
├── LICENSE               MIT License
└── web/
    ├── render_time.js    ComfyUI extension + Render Time node UI
    ├── timing_panel.js   Timing table and display components
    └── metadata_helper.js PNG/workflow metadata utilities
```

---

## Requirements

- ComfyUI `>= 0.3.0`
- Python `>= 3.10`
- [Pillow](https://pypi.org/project/Pillow/) — for `WORKFLOW.png` generation

---

## License

MIT License — © 2026 Ramiro Montes De Oca  
See [LICENSE](LICENSE) for full text.

---

## Links

- GitHub: [https://github.com/videofeedback/comfyui-render-time](https://github.com/videofeedback/comfyui-render-time)
- ComfyUI Registry: [registry.comfy.org](https://registry.comfy.org)
