/**
 * Render Time — render_time.js
 * Author: Ramiro Montes De Oca
 * GitHub: https://github.com/videofeedback/comfyui-render-time
 *
 * ComfyUI node extension: "Render Time"
 * Extension ID: Comfy.RenderTime
 * Category: Metadata
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ─── Shared state ─────────────────────────────────────────────────────────────

// Per-workflow-tab entry store.
// Key: the ComfyWorkflow object from app.workflowManager.activeWorkflow — one
// stable object per tab that outlives graph configure/clear cycles.
// Value: the last timing report array for that tab, or one legacy unscoped entry.
const _wfEntries = new WeakMap();
const RENDER_TIME_TITLE = "⭐Render Time (ComfyCode)⭐";

function _activeWorkflow() {
    try { return app.workflowManager?.activeWorkflow ?? null; } catch (_) { return null; }
}

function normalizeRenderTimeTitle(node) {
    try {
        node.title = RENDER_TIME_TITLE;
        if (node._meta) node._meta.title = RENDER_TIME_TITLE;
    } catch (_) {}
}

let _notifyEnabled  = true;
const _nodeInstances = new Set();   // Set<WeakRef<LGraphNode>>

// Table sort state (shared across all node instances — they all show the same data)
let _sortCol = "time";   // "exec" | "id" | "type" | "title" | "time"
let _sortDir = "desc";   // "asc" | "desc"

// ─── Audio chime (Web Audio API — no external file) ──────────────────────────

function playChime() {
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        [523.25, 659.25, 783.99, 1046.50].forEach((freq, i) => {
            const osc  = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.type = "sine";
            osc.frequency.value = freq;
            const t = ctx.currentTime + i * 0.18;
            gain.gain.setValueAtTime(0, t);
            gain.gain.linearRampToValueAtTime(0.25, t + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.001, t + 0.7);
            osc.start(t);
            osc.stop(t + 0.7);
        });
    } catch (_) {}
}

// ─── Graph injection (Ctrl+S persistence) ────────────────────────────────────

function injectIntoGraph(renderTimeReport) {
    try {
        if (!app.graph) return;
        if (!app.graph.extra) app.graph.extra = {};
        app.graph.extra.render_time_report = renderTimeReport;
    } catch (_) {}
}

// ─── JS-side workflow save (fallback) ────────────────────────────────────────

async function saveTimedWorkflow(promptId) {
    const name = app.workflowManager?.activeWorkflow?.name
              || document.title.replace(/\s*[-|].*$/, "").trim()
              || "workflow";
    try {
        await fetch("/render-time/save-workflow", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt_id: promptId, workflow_name: name }),
        });
    } catch (_) {}
}

// ─── Sort helpers ─────────────────────────────────────────────────────────────

function _sortIndicator(col) {
    if (_sortCol !== col) return `<span style="color:#444;margin-left:2px;font-size:8px">⇅</span>`;
    return _sortDir === "asc"
        ? `<span style="color:#4a9eff;margin-left:2px;font-size:8px">▲</span>`
        : `<span style="color:#4a9eff;margin-left:2px;font-size:8px">▼</span>`;
}

function _thStyle(col, align) {
    const active = _sortCol === col;
    const base = `padding:3px 5px;text-align:${align ?? "left"};cursor:pointer;user-select:none;white-space:nowrap;border-bottom:1px solid #333`;
    return active ? `${base};color:#4a9eff` : `${base};color:#666`;
}

// ─── Time formatting ─────────────────────────────────────────────────────────

/**
 * Format seconds into a concise Hh MMm SSs string.
 * Examples: 45.3 → "45s"  |  90 → "1m 30s"  |  3723 → "1h 02m 03s"
 */
function _fmtHMS(sec) {
    const total = Math.floor(sec);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s`;
    if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`;
    return `${s}s`;
}

function _escapeHTML(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function buildPreviewHTML(entry) {
    const imagePreview = entry?.preview_image;
    const videoPreview = entry?.preview_video;
    const preview = imagePreview ?? videoPreview;
    if (!preview?.view_url) return "";
    const isVideoPreview = preview === videoPreview;
    const filename = _escapeHTML(preview.filename ?? (isVideoPreview ? "output.mp4" : "output.png"));
    const subfolder = preview.subfolder ? _escapeHTML(preview.subfolder) : "";
    const src = _escapeHTML(preview.view_url);
    const format = String(preview.format ?? (isVideoPreview ? "mp4" : "png")).toLowerCase();
    const meta = subfolder
        ? `<span style="color:#666;font-size:10px">${subfolder}/${filename}</span>`
        : `<span style="color:#666;font-size:10px">${filename}</span>`;
    const mediaHTML = isVideoPreview
        ? `<video controls preload="metadata" muted playsinline
            style="display:block;width:100%;max-height:220px;background:#000;object-fit:contain">
            <source src="${src}" type="${format === "mp4" ? "video/mp4" : `video/${_escapeHTML(format)}`}">
          </video>`
        : `<img src="${src}" alt="${filename}"
            style="display:block;width:100%;max-height:220px;background:#000;object-fit:contain">`;
    return `
      <div style="margin-bottom:10px">
        <div style="color:#ccc;font-weight:bold;font-size:11px;margin-bottom:5px">Output Preview</div>
        <div style="border:1px solid #2f2f2f;border-radius:6px;overflow:hidden;background:#0c0c0c">
          ${mediaHTML}
        </div>
        <div style="margin-top:4px">${meta}</div>
      </div>`;
}

// ─── Timing tab HTML ─────────────────────────────────────────────────────────

function buildTimingHTML(entry) {
    if (!entry) return `<div style="color:#555;font-size:11px;padding:8px">Waiting for first run…</div>`;

    const totalSec  = entry.total_sec ?? 0;
    const cfg       = entry.machine_config ?? {};
    const flags     = (entry.launch_flags ?? []).join(" ") || "(none)";
    const nodesObj  = entry.nodes ?? {};
    const wfAuthor  = entry.workflow_author  ? `<br><span style="color:#888">Author:</span> ${entry.workflow_author}` : "";
    const wfContact = entry.workflow_contact ? `<br><span style="color:#888">Contact:</span> ${entry.workflow_contact}` : "";
    const previewHTML = buildPreviewHTML(entry);

    // Sort node IDs according to current sort column & direction
    const nodeOrder = Object.keys(nodesObj).sort((a, b) => {
        const ra = nodesObj[a], rb = nodesObj[b];
        let valA, valB;
        switch (_sortCol) {
            case "exec":
                valA = ra.exec_order ?? 9999;
                valB = rb.exec_order ?? 9999;
                return _sortDir === "asc" ? valA - valB : valB - valA;
            case "id":
                valA = parseInt(a, 10);
                valB = parseInt(b, 10);
                return _sortDir === "asc" ? valA - valB : valB - valA;
            case "type":
                valA = ra.type  ?? "";
                valB = rb.type  ?? "";
                return _sortDir === "asc" ? valA.localeCompare(valB) : valB.localeCompare(valA);
            case "title":
                valA = ra.title ?? "";
                valB = rb.title ?? "";
                return _sortDir === "asc" ? valA.localeCompare(valB) : valB.localeCompare(valA);
            case "time":
            default:
                // Cached nodes always sink to the bottom when sorting by time
                if (ra.cached !== rb.cached) return ra.cached ? 1 : -1;
                valA = ra.duration_sec ?? 0;
                valB = rb.duration_sec ?? 0;
                return _sortDir === "asc" ? valA - valB : valB - valA;
        }
    });

    let rowsHTML = "";
    let rank = 1;
    for (const nid of nodeOrder) {
        const n    = nodesObj[nid];
        const dur  = n.duration_sec ?? 0;
        const pct  = totalSec > 0 ? ((dur / totalSec) * 100).toFixed(1) : "0.0";
        const barW = totalSec > 0 ? Math.max(1, Math.round((dur / totalSec) * 100)) : 0;
        const bar  = n.cached
            ? `<span style="color:#555;font-size:10px">cached</span>`
            : `<span style="display:inline-block;width:${barW}%;height:5px;background:#4a9eff;border-radius:2px;vertical-align:middle"></span>`;
        const execNum = n.exec_order != null && n.exec_order !== 9999 ? n.exec_order + 1 : (n.cached ? "–" : rank);
        if (!n.cached) rank++;
        rowsHTML += `
          <tr data-nodeid="${nid}" style="cursor:pointer" title="Click to highlight node ${nid} in canvas">
            <td style="padding:2px 5px;color:#777">${execNum}</td>
            <td style="padding:2px 5px;color:#888">${nid}</td>
            <td style="padding:2px 5px;color:#ccc;max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${n.type ?? ""}">${n.type ?? ""}</td>
            <td style="padding:2px 5px;color:#fff;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${n.title ?? ""}">${n.title ?? ""}</td>
            <td style="padding:2px 5px;text-align:right;color:#4a9eff">${dur.toFixed(3)}s</td>
            <td style="padding:2px 5px;text-align:right;color:#777">${pct}%</td>
            <td style="padding:2px 5px">${bar}</td>
          </tr>`;
    }

    let settingsHTML = "";
    for (const nid of nodeOrder) {
        const n  = nodesObj[nid];
        const ss = n.settings ?? {};
        if (!Object.keys(ss).length) continue;
        settingsHTML += `
          <details style="margin-bottom:4px">
            <summary style="cursor:pointer;color:#888;font-size:10px">
              Node ${nid} — ${n.type ?? ""}${n.cached ? " (cached)" : ""}
            </summary>
            <table style="font-size:10px;border-collapse:collapse;margin-top:2px">
              ${Object.entries(ss).map(([k,v]) =>
                `<tr><td style="padding:1px 8px;color:#666;width:130px">${k}</td><td style="color:#ccc">${v}</td></tr>`
              ).join("")}
            </table>
          </details>`;
    }

    return `
      ${previewHTML}
      <div style="font-size:11px;color:#bbb;margin-bottom:8px;line-height:1.5">
        <span style="color:#888">GPU:</span> ${cfg.gpu ?? "?"} &nbsp;
        <span style="color:#888">VRAM:</span> ${cfg.vram_gb ?? "?"}GB &nbsp;
        <span style="color:#888">RAM:</span> ${cfg.ram_gb ?? "?"}GB<br>
        <span style="color:#888">PyTorch:</span> ${cfg.pytorch_version ?? "?"} &nbsp;
        <span style="color:#888">ComfyUI:</span> ${cfg.comfyui_version ?? "?"}
        ${wfAuthor}${wfContact}<br>
        <span style="color:#888">Flags:</span> <code style="font-size:9px;color:#666">${flags}</code>
      </div>
      <div style="margin-bottom:8px;color:#fff;font-weight:bold;font-size:12px">
        ⏱ ${totalSec.toFixed(2)}s
        <span style="color:#666;font-weight:normal;font-size:11px;margin-left:4px">· ${_fmtHMS(totalSec)}</span>
        &nbsp;
        <span style="color:#888;font-weight:normal;font-size:11px">${nodeOrder.length} nodes</span>
      </div>
      <div style="overflow-x:auto;margin-bottom:10px">
        <table style="width:100%;border-collapse:collapse;font-size:10px">
          <thead>
            <tr>
              <th data-sortcol="exec"  style="${_thStyle("exec")}"  title="Sort by execution order"># ${_sortIndicator("exec")}</th>
              <th data-sortcol="id"    style="${_thStyle("id")}"    title="Sort by node ID">ID ${_sortIndicator("id")}</th>
              <th data-sortcol="type"  style="${_thStyle("type")}"  title="Sort by node type">Type ${_sortIndicator("type")}</th>
              <th data-sortcol="title" style="${_thStyle("title")}" title="Sort by node title">Title ${_sortIndicator("title")}</th>
              <th data-sortcol="time"  style="${_thStyle("time","right")}" title="Sort by duration">Time ${_sortIndicator("time")}</th>
              <th style="padding:3px 5px;color:#555;text-align:right">%</th>
              <th style="padding:3px 5px;color:#555">Bar</th>
            </tr>
          </thead>
          <tbody>${rowsHTML}</tbody>
        </table>
      </div>
      ${settingsHTML ? `<details><summary style="cursor:pointer;color:#666;font-size:10px;margin-bottom:4px">▶ Node Settings</summary><div style="margin-top:4px">${settingsHTML}</div></details>` : ""}`;
}

// ─── Settings tab HTML ────────────────────────────────────────────────────────

function buildSettingsHTML(cfg) {
    function locSelect(key, cur) {
        const label = {
            embed_json: "Default (output/)",
            txt_report: "Default (output/)",
            isolated_json: "Default (output/)",
            workflow_png: "Default (output/)",
            workflow_mp4: "Default (output/)",
        }[key] ?? "Default";
        return `<select id="tr-loc-${key}" onchange="window._trLocChange('${key}')"
            style="width:100%;background:#1a1a1a;color:#ccc;border:1px solid #444;border-radius:3px;padding:2px 5px;font-size:10px;margin-top:3px">
          <option value="default" ${cur !== "custom" ? "selected" : ""}>${label}</option>
          <option value="custom"  ${cur === "custom"  ? "selected" : ""}>Custom folder…</option>
        </select>`;
    }
    function outputRow(key, label, desc) {
        const e = cfg[key] ?? { enabled: true, location: "default", custom_path: "" };
        return `
          <div style="border:1px solid #2a2a2a;border-radius:4px;padding:7px 9px;margin-bottom:8px">
            <label style="display:flex;align-items:center;gap:6px;cursor:pointer;margin-bottom:3px">
              <input type="checkbox" id="tr-en-${key}" ${e.enabled ? "checked" : ""}
                style="accent-color:#4a9eff;width:12px;height:12px">
              <span style="color:#ccc;font-size:11px;font-weight:bold">${label}</span>
            </label>
            <div style="color:#555;font-size:10px;margin-bottom:4px">${desc}</div>
            ${locSelect(key, e.location)}
            <div id="tr-cp-${key}" style="${e.location === "custom" ? "" : "display:none"};margin-top:4px">
              <input id="tr-custom-${key}" type="text" value="${_escapeHTML(e.custom_path ?? "")}"
                placeholder="Absolute path…"
                style="width:100%;box-sizing:border-box;background:#111;color:#ccc;border:1px solid #444;
                       border-radius:3px;padding:3px 6px;font-size:10px;font-family:monospace">
            </div>
          </div>`;
    }

    const authorVal  = _escapeHTML(cfg.workflow_author ?? "");
    const contactVal = _escapeHTML(cfg.workflow_contact ?? "");
    const mp4Cfg = cfg.workflow_mp4 ?? { location: "default", custom_path: "" };
    const namingCfg = cfg.output_naming ?? {};
    const titleMode = namingCfg.title_mode === "custom" ? "custom" : "default";
    const extraModes = new Set(["default_t_ymd_hms_w", "t_w", "t", "w", "custom", "none"]);
    const extraMode = extraModes.has(namingCfg.extra_mode) ? namingCfg.extra_mode : "default_t_ymd_hms_w";
    const customTitleVal = _escapeHTML(namingCfg.custom_title ?? "");
    const customExtraVal = _escapeHTML(namingCfg.custom_extra ?? "");
    const videoMetaChk = cfg.video_metadata_enabled !== false ? "checked" : "";
    const notifyChk  = cfg.notify_on_complete !== false ? "checked" : "";

    return `
      <div style="font-size:11px;font-family:monospace">
        <div style="border:1px solid #2a2a2a;border-radius:4px;padding:7px 9px;margin-bottom:8px">
          <div style="color:#ccc;font-weight:bold;font-size:11px;margin-bottom:5px">Workflow Identity</div>
          <div style="color:#555;font-size:10px;margin-bottom:6px">Shown in .md / .txt reports and embedded JSON.</div>
          <label style="color:#888;font-size:10px;display:block;margin-bottom:2px">Workflow Author:</label>
          <input id="tr-wf-author" type="text" value="${authorVal}" placeholder="e.g. Jane Smith"
            style="width:100%;box-sizing:border-box;background:#111;color:#ccc;border:1px solid #444;border-radius:3px;padding:3px 6px;font-size:10px;margin-bottom:6px;font-family:monospace">
          <label style="color:#888;font-size:10px;display:block;margin-bottom:2px">Contact:</label>
          <input id="tr-wf-contact" type="text" value="${contactVal}" placeholder="e.g. email or @handle"
            style="width:100%;box-sizing:border-box;background:#111;color:#ccc;border:1px solid #444;border-radius:3px;padding:3px 6px;font-size:10px;font-family:monospace">
        </div>

        <div style="border:1px solid #2a2a2a;border-radius:4px;padding:7px 9px;margin-bottom:8px">
          <div style="color:#ccc;font-weight:bold;font-size:11px;margin-bottom:5px">Output File Naming</div>
          <div style="color:#555;font-size:10px;margin-bottom:6px">Controls the shared filename prefix used by JSON, TXT, PNG, and Render Time MP4 outputs.</div>
          <label style="color:#888;font-size:10px;display:block;margin-bottom:2px">Title:</label>
          <select id="tr-name-title-mode" onchange="window._trNamingChange()"
            style="width:100%;background:#1a1a1a;color:#ccc;border:1px solid #444;border-radius:3px;padding:2px 5px;font-size:10px">
            <option value="default" ${titleMode !== "custom" ? "selected" : ""}>(Default) Keep the Original Title</option>
            <option value="custom" ${titleMode === "custom" ? "selected" : ""}>(Custom) Add a custom title</option>
          </select>
          <div id="tr-name-custom-title-wrap" style="${titleMode === "custom" ? "" : "display:none"};margin-top:4px">
            <input id="tr-name-custom-title" type="text" value="${customTitleVal}" placeholder="Custom title..."
              style="width:100%;box-sizing:border-box;background:#111;color:#ccc;border:1px solid #444;border-radius:3px;padding:3px 6px;font-size:10px;font-family:monospace">
          </div>

          <label style="color:#888;font-size:10px;display:block;margin-top:8px;margin-bottom:2px">Add Extra:</label>
          <select id="tr-name-extra-mode" onchange="window._trNamingChange()"
            style="width:100%;background:#1a1a1a;color:#ccc;border:1px solid #444;border-radius:3px;padding:2px 5px;font-size:10px">
            <option value="default_t_ymd_hms_w" ${extraMode === "default_t_ymd_hms_w" ? "selected" : ""}>(Default T+YMD+HMS+W) [TITLE]-[YYYYMMDD-HHMMSS-WORKFLOW#]</option>
            <option value="t_w" ${extraMode === "t_w" ? "selected" : ""}>(T+W) [TITLE]-[WORKFLOW#]</option>
            <option value="t" ${extraMode === "t" ? "selected" : ""}>(T) [TITLE]</option>
            <option value="w" ${extraMode === "w" ? "selected" : ""}>(W) [WORKFLOW#]</option>
            <option value="custom" ${extraMode === "custom" ? "selected" : ""}>(CUSTOM) [CUSTOM]</option>
            <option value="none" ${extraMode === "none" ? "selected" : ""}>(NONE)</option>
          </select>
          <div id="tr-name-custom-extra-wrap" style="${extraMode === "custom" ? "" : "display:none"};margin-top:4px">
            <input id="tr-name-custom-extra" type="text" value="${customExtraVal}" placeholder="Custom filename text..."
              style="width:100%;box-sizing:border-box;background:#111;color:#ccc;border:1px solid #444;border-radius:3px;padding:3px 6px;font-size:10px;font-family:monospace">
          </div>
        </div>

        ${outputRow("embed_json",    "Embedded report in JSON Workflow",              "Saves the workflow JSON with timing embedded in extra.render_time_report — uses the dated workflow filename.")}
        ${outputRow("txt_report",    "Generate TXT report",                          "Saves a plain-text timing report to the output folder — same name as the dated workflow file, .txt extension.")}
        ${outputRow("isolated_json", "Generate isolated JSON Render Time Report",    "Saves a standalone JSON timing file (not a workflow) with all machine info, render time, and node data.")}
        ${outputRow("workflow_png",  "Embedded Workflow PNG",                        "Saves a PNG using the first video/image frame as thumbnail. Full workflow JSON embedded — drag into ComfyUI to reload.")}

        <div style="border:1px solid #2a2a2a;border-radius:4px;padding:7px 9px;margin-bottom:8px">
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
            <input type="checkbox" id="tr-video-meta" ${videoMetaChk} style="accent-color:#4a9eff;width:12px;height:12px">
            <span style="color:#ccc;font-size:11px;font-weight:bold">Embed metadata in saved MP4 video</span>
          </label>
          <div style="color:#555;font-size:10px;margin-top:3px;margin-left:18px">Creates a Render Time managed MP4 with embedded workflow and log using the Output File Naming rules above.</div>
          <div style="margin-top:6px;margin-left:18px">
            ${locSelect("workflow_mp4", mp4Cfg.location)}
            <div id="tr-cp-workflow_mp4" style="${mp4Cfg.location === "custom" ? "" : "display:none"};margin-top:4px">
              <input id="tr-custom-workflow_mp4" type="text" value="${_escapeHTML(mp4Cfg.custom_path ?? "")}"
                placeholder="Absolute path..."
                style="width:100%;box-sizing:border-box;background:#111;color:#ccc;border:1px solid #444;
                       border-radius:3px;padding:3px 6px;font-size:10px;font-family:monospace">
            </div>
          </div>
        </div>

        <div style="border:1px solid #2a2a2a;border-radius:4px;padding:7px 9px;margin-bottom:8px">
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
            <input type="checkbox" id="tr-notify" ${notifyChk} style="accent-color:#4a9eff;width:12px;height:12px">
            <span style="color:#ccc;font-size:11px;font-weight:bold">Notify when render is finished</span>
          </label>
          <div style="color:#555;font-size:10px;margin-top:3px;margin-left:18px">Plays a chime sound when the report is written.</div>
        </div>

        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <button id="tr-refresh-cfg"
            style="background:#2a2a2a;color:#fff;border:1px solid #444;border-radius:4px;padding:5px 14px;font-size:11px;cursor:pointer;font-family:monospace">
            ↺ Refresh
          </button>
          <span style="color:#555;font-size:10px">Reload config from disk</span>
        </div>

        <div style="display:flex;align-items:center;gap:8px">
          <button id="tr-save-cfg"
            style="background:#4a9eff;color:#fff;border:none;border-radius:4px;padding:5px 14px;font-size:11px;cursor:pointer;font-family:monospace">
            Apply &amp; Save
          </button>
          <span id="tr-cfg-status" style="color:#888;font-size:10px"></span>
        </div>
      </div>`;
}

// ─── Settings listeners ───────────────────────────────────────────────────────

function attachSettingsListeners(body) {
    window._trLocChange = (key) => {
        const sel  = document.getElementById(`tr-loc-${key}`);
        const wrap = document.getElementById(`tr-cp-${key}`);
        if (sel && wrap) wrap.style.display = sel.value === "custom" ? "" : "none";
    };
    window._trNamingChange = () => {
        const titleModeSel = document.getElementById("tr-name-title-mode");
        const titleWrap = document.getElementById("tr-name-custom-title-wrap");
        if (titleModeSel && titleWrap) {
            titleWrap.style.display = titleModeSel.value === "custom" ? "" : "none";
        }
        const extraModeSel = document.getElementById("tr-name-extra-mode");
        const extraWrap = document.getElementById("tr-name-custom-extra-wrap");
        if (extraModeSel && extraWrap) {
            extraWrap.style.display = extraModeSel.value === "custom" ? "" : "none";
        }
    };
    window._trNamingChange();

    // Refresh config button — re-loads settings from disk
    const refreshCfg = document.getElementById("tr-refresh-cfg");
    if (refreshCfg && body) {
        refreshCfg.addEventListener("click", () => _loadSettingsIntoBody(body));
    }

    const btn    = document.getElementById("tr-save-cfg");
    const status = document.getElementById("tr-cfg-status");
    if (!btn) return;

    btn.addEventListener("click", async () => {
        const newCfg = {};
        for (const key of ["embed_json", "txt_report", "isolated_json", "workflow_png"]) {
            newCfg[key] = {
                enabled:     document.getElementById(`tr-en-${key}`)?.checked ?? true,
                location:    document.getElementById(`tr-loc-${key}`)?.value  ?? "default",
                custom_path: document.getElementById(`tr-custom-${key}`)?.value.trim() ?? "",
            };
        }
        newCfg.workflow_mp4 = {
            location:    document.getElementById("tr-loc-workflow_mp4")?.value ?? "default",
            custom_path: document.getElementById("tr-custom-workflow_mp4")?.value.trim() ?? "",
        };
        newCfg.output_naming = {
            title_mode: document.getElementById("tr-name-title-mode")?.value ?? "default",
            custom_title: document.getElementById("tr-name-custom-title")?.value.trim() ?? "",
            extra_mode: document.getElementById("tr-name-extra-mode")?.value ?? "default_t_ymd_hms_w",
            custom_extra: document.getElementById("tr-name-custom-extra")?.value.trim() ?? "",
        };
        newCfg.workflow_author  = document.getElementById("tr-wf-author")?.value.trim()  ?? "";
        newCfg.workflow_contact = document.getElementById("tr-wf-contact")?.value.trim() ?? "";
        newCfg.video_metadata_enabled = document.getElementById("tr-video-meta")?.checked ?? true;
        newCfg.notify_on_complete = document.getElementById("tr-notify")?.checked ?? true;
        _notifyEnabled = newCfg.notify_on_complete;

        try {
            btn.disabled = true;
            if (status) status.textContent = "Saving…";
            const resp = await fetch("/render-time/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(newCfg),
            });
            const data = resp.ok ? await resp.json() : {};
            if (status) {
                status.style.color   = data.saved ? "#4f4" : "#f66";
                status.textContent   = data.saved ? "Saved ✓" : (data.error ?? "Error");
            }
        } catch (e) {
            if (status) { status.style.color = "#f66"; status.textContent = String(e); }
        } finally {
            btn.disabled = false;
            setTimeout(() => { if (status) { status.textContent = ""; status.style.color = "#888"; } }, 3000);
        }
    });
}

// ─── Click-to-highlight ───────────────────────────────────────────────────────

function attachNodeClickHandlers(body) {
    body.addEventListener("click", (e) => {
        const row = e.target.closest("tr[data-nodeid]");
        if (!row) return;
        const nodeId = parseInt(row.dataset.nodeid, 10);
        if (isNaN(nodeId)) return;
        try {
            const graphNode = app.graph.getNodeById(nodeId);
            if (graphNode) {
                app.canvas.centerOnNode(graphNode);
                app.canvas.selectNode(graphNode, false);
            }
        } catch (_) {}
    });
}

// ─── Sort header click handler ────────────────────────────────────────────────

function attachSortHandlers(body) {
    body.querySelector("table")?.addEventListener("click", (e) => {
        const th = e.target.closest("th[data-sortcol]");
        if (!th) return;
        const col = th.dataset.sortcol;
        if (_sortCol === col) {
            _sortDir = _sortDir === "asc" ? "desc" : "asc";
        } else {
            _sortCol = col;
            _sortDir = col === "time" ? "desc" : "asc";
        }
        // Re-render each node with ITS OWN entry — never mix data across tabs
        for (const ref of [..._nodeInstances]) {
            const node = ref.deref();
            if (!node) { _nodeInstances.delete(ref); continue; }
            const container = node.timingWidget?.element;
            if (!container) continue;
            if (container.dataset.activeTab === "timing") {
                const b = container.querySelector(".tr-body");
                if (b) renderTimingBody(b, node._rtEntry ?? null);
            }
        }
    });
}

// ─── Render timing body ───────────────────────────────────────────────────────

function renderTimingBody(body, entry) {
    body.innerHTML = buildTimingHTML(entry ?? null);
    attachNodeClickHandlers(body);
    attachSortHandlers(body);
}

// ─── Per-node timing entry helpers ───────────────────────────────────────────

/**
 * Read the timing entry embedded in graph.extra for THIS node's own graph.
 * This is the authoritative per-tab source: set by injectIntoGraph() on each
 * run, and preserved through ComfyUI's serialize/configure tab-switch cycle.
 */
function _getGraphTimingEntry(node) {
    try {
        const reports = node.graph?.extra?.render_time_report;
        if (Array.isArray(reports) && reports.length > 0) {
            return _entryFromReportsForNode(reports, node);
        }
    } catch (_) {}
    return null;
}

function _entryFromReportsForNode(reports, node) {
    if (!Array.isArray(reports) || !node) return null;
    const scoped = reports.find((entry) =>
        String(entry?.render_node_id ?? "") === String(node.id)
    );
    if (scoped) return scoped;

    // Legacy reports did not have render_node_id. Use them only when there is a
    // single unscoped entry, otherwise showing the last entry leaks previews
    // between independent Render Time nodes.
    if (reports.length === 1 && reports[0]?.render_node_id == null) {
        return reports[0];
    }
    return null;
}

/**
 * Best entry for a node.  Three sources, in priority order:
 *  1. graph.extra           — set by injectIntoGraph(), per-graph isolated
 *  2. _wfEntries (WeakMap)  — keyed by ComfyWorkflow object, survives graph
 *                             configure/clear cycles on tab switch
 *  3. node._rtEntry         — fast in-session cache
 */
function _getEntryForNode(node) {
    const graphEntry = _getGraphTimingEntry(node);
    if (graphEntry || _graphHasRenderTimeReports(node)) return graphEntry;

    const wf = _activeWorkflow();
    const wfEntry = wf ? _wfEntries.get(wf) : null;
    const workflowEntry = Array.isArray(wfEntry)
        ? _entryFromReportsForNode(wfEntry, node)
        : _entryMatchesNode(wfEntry, node);
    return workflowEntry
        ?? node._rtEntry
        ?? null;
}

function _graphHasRenderTimeReports(node) {
    try {
        return Array.isArray(node.graph?.extra?.render_time_report)
            && node.graph.extra.render_time_report.length > 0;
    } catch (_) {
        return false;
    }
}

function _entryMatchesNode(entry, node) {
    if (!entry || !node) return null;
    if (entry.render_node_id == null) return entry;
    return String(entry.render_node_id) === String(node.id) ? entry : null;
}

/**
 * Re-render a node's timing body, reading from all authoritative sources.
 */
function _refreshNodeBody(node) {
    const container = node.timingWidget?.element;
    if (!container) return;
    if (container.dataset.activeTab !== "timing") return;
    const body = container.querySelector(".tr-body");
    if (!body) return;
    const entry = _getEntryForNode(node);
    if (entry) node._rtEntry = entry;
    renderTimingBody(body, node._rtEntry ?? null);
}

// ─── Per-node render helpers ─────────────────────────────────────────────────

function _setTab(container, tab, node) {
    container.dataset.activeTab = tab;

    const tBtn = container.querySelector(".tr-tab-timing");
    const sBtn = container.querySelector(".tr-tab-settings");
    const body = container.querySelector(".tr-body");
    if (!body) return;

    const act   = "padding:3px 10px;font-size:10px;cursor:pointer;border:none;border-radius:3px;background:#4a9eff;color:#fff;font-family:monospace";
    const inact = "padding:3px 10px;font-size:10px;cursor:pointer;border:none;border-radius:3px;background:#2a2a2a;color:#888;font-family:monospace";
    if (tBtn) tBtn.style.cssText = (tab === "timing")   ? act : inact;
    if (sBtn) sBtn.style.cssText = (tab === "settings") ? act : inact;

    if (tab === "timing") {
        _refreshNodeBody(node);
    } else {
        _loadSettingsIntoBody(body);
    }
}

async function _loadSettingsIntoBody(body) {
    body.innerHTML = `<em style="color:#555;font-size:10px">Loading…</em>`;
    try {
        const resp = await fetch("/render-time/config");
        const text = await resp.text();
        if (!text) throw new Error("Empty response");
        const cfg  = JSON.parse(text);
        _notifyEnabled = cfg.notify_on_complete !== false;
        body.innerHTML = buildSettingsHTML(cfg);
        attachSettingsListeners(body);
    } catch (e) {
        body.innerHTML = `<span style="color:#f66;font-size:10px">Could not load settings: ${e}</span>`;
    }
}

// ─── Properties Panel helpers ─────────────────────────────────────────────────

const _SYNCED_PROPS = new Set([
    "workflow_author", "workflow_contact", "notify_on_complete", "video_metadata_enabled",
    "embed_json", "isolated_json", "workflow_png",
]);

function _applyConfigToProperties(node, cfg) {
    try {
        if (cfg.workflow_author  !== undefined) node.setProperty("workflow_author",    cfg.workflow_author);
        if (cfg.workflow_contact !== undefined) node.setProperty("workflow_contact",   cfg.workflow_contact);
        if (cfg.video_metadata_enabled !== undefined) node.setProperty("video_metadata_enabled", cfg.video_metadata_enabled !== false);
        if (cfg.notify_on_complete !== undefined) node.setProperty("notify_on_complete", cfg.notify_on_complete !== false);
        if (cfg.embed_json)    node.setProperty("embed_json",    cfg.embed_json.enabled    !== false);
        if (cfg.isolated_json) node.setProperty("isolated_json", cfg.isolated_json.enabled !== false);
        if (cfg.workflow_png)  node.setProperty("workflow_png",  cfg.workflow_png.enabled  !== false);
    } catch (_) {}
}

// ─── Build the DOM skeleton for a node ───────────────────────────────────────

let _uidCounter = 0;

function buildNodeContainer(node) {
    const uid = `tr-node-${++_uidCounter}`;
    const container = document.createElement("div");
    container.dataset.uid       = uid;
    container.dataset.activeTab = "timing";
    container.style.cssText = `
        width: 100%;
        height: 100%;
        display: flex;
        flex-direction: column;
        background: #1a1a1a;
        border-radius: 4px;
        overflow: hidden;
        font-family: monospace;
        box-sizing: border-box;
    `;

    // Tab bar
    const tabs = document.createElement("div");
    tabs.style.cssText = "display:flex;gap:5px;padding:6px 8px;background:#222;border-bottom:1px solid #333;flex-shrink:0;align-items:center";
    tabs.innerHTML = `
        <button class="tr-tab-timing"
            style="padding:3px 10px;font-size:10px;cursor:pointer;border:none;border-radius:3px;background:#4a9eff;color:#fff;font-family:monospace">
            ⏱ Timing
        </button>
        <button class="tr-tab-settings"
            style="padding:3px 10px;font-size:10px;cursor:pointer;border:none;border-radius:3px;background:#2a2a2a;color:#888;font-family:monospace">
            ⚙ Settings
        </button>`;

    tabs.querySelector(".tr-tab-timing").addEventListener("click",   () => _setTab(container, "timing",   node));
    tabs.querySelector(".tr-tab-settings").addEventListener("click", () => _setTab(container, "settings", node));

    // Refresh button — pushed to the right, white and visible
    const refreshBtn = document.createElement("button");
    refreshBtn.className = "tr-tab-refresh";
    refreshBtn.title = "Reload timing data from this workflow";
    refreshBtn.style.cssText =
        "margin-left:auto;padding:3px 8px;font-size:11px;cursor:pointer;" +
        "border:1px solid #444;border-radius:3px;background:#2a2a2a;color:#fff;" +
        "font-family:monospace";
    refreshBtn.textContent = "↺ Refresh";
    refreshBtn.onmouseenter = () => { refreshBtn.style.borderColor = "#4a9eff"; };
    refreshBtn.onmouseleave = () => { refreshBtn.style.borderColor = "#444"; };

    refreshBtn.addEventListener("click", () => {
        if (container.dataset.activeTab !== "timing") return;
        const body = container.querySelector(".tr-body");
        if (!body) return;
        const entry = _getEntryForNode(node);
        node._rtEntry = entry ?? null;
        renderTimingBody(body, node._rtEntry);
    });

    tabs.appendChild(refreshBtn);

    // Scrollable body
    const body = document.createElement("div");
    body.className = "tr-body";
    body.style.cssText = "padding:8px 10px;overflow-y:auto;flex:1";

    container.appendChild(tabs);
    container.appendChild(body);

    // Populate immediately from this node's own entry (or graph-embedded data).
    // Shows "Waiting…" only when truly nothing has run yet for this workflow tab.
    const graphEntry = _getGraphTimingEntry(node);
    if (graphEntry) node._rtEntry = graphEntry;
    renderTimingBody(body, node._rtEntry ?? null);

    return container;
}

// ─── Backend fetch (used only by fetchLatestIntoNode — kept as utility) ──────

async function fetchLatestIntoNode(node) {
    try {
        const resp = await fetch("/render-time/latest/full");
        if (!resp.ok) return;
        const data = await resp.json();
        if (data && !data.error) {
            node._rtEntry = data;
            const container = node.timingWidget?.element;
            if (!container) return;
            if (container.dataset.activeTab === "timing") {
                const body = container.querySelector(".tr-body");
                if (body) renderTimingBody(body, node._rtEntry);
            }
        }
    } catch (_) {}
}

// ─── Extension ───────────────────────────────────────────────────────────────

app.registerExtension({
    name: "Comfy.RenderTime",

    // Shows a "Render Time" group in Settings → Extensions under "Metadata"
    settings: [
        {
            id:           "Comfy.RenderTime.version",
            name:         "Render Time v1.0.0",
            category:     ["Metadata", "Render Time"],
            type:         "hidden",
            defaultValue: "1.0.0",
        },
    ],

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "RenderTime") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onCreated?.apply(this, arguments);
            normalizeRenderTimeTitle(this);

            this.serialize_widgets = true;
            this.setSize([520, 380]);
            this.resizable = true;

            const container = buildNodeContainer(this);

            this.timingWidget = this.addDOMWidget(
                "timing_display",
                "div",
                container,
                {
                    getValue()       { return ""; },
                    setValue()       {},
                    hideOnZoom:      false,
                    getMinHeight()   { return 220; },
                }
            );

            // ── Properties Panel — info fields (read-only display) ──────────
            this.properties ??= {};
            Object.assign(this.properties, {
                description:  "Displays per-run execution timing and metadata.",
                connections:  "None needed",
                github:       "https://github.com/videofeedback/comfyui-render-time",
                version:      "v1.0.0",
            });

            // ── Properties Panel — editable settings (synced with config.json)
            this.addProperty("workflow_author",    "", "string");
            this.addProperty("workflow_contact",   "", "string");
            this.addProperty("video_metadata_enabled", true, "boolean");
            this.addProperty("notify_on_complete", true, "boolean");
            this.addProperty("embed_json",         true, "boolean");
            this.addProperty("isolated_json",      true, "boolean");
            this.addProperty("workflow_png",       true, "boolean");

            // Pre-populate properties from current config
            fetch("/render-time/config")
                .then(r => r.json())
                .then(cfg => _applyConfigToProperties(this, cfg))
                .catch(() => {});

            // Sync property changes back to config.json
            const origChanged = this.onPropertyChanged;
            this.onPropertyChanged = function(name, value) {
                origChanged?.apply(this, arguments);
                if (!_SYNCED_PROPS.has(name)) return;
                if (name === "notify_on_complete") _notifyEnabled = value;
                fetch("/render-time/config/property", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ key: name, value }),
                }).catch(() => {});
            };

            // Register for live WS updates
            _nodeInstances.add(new WeakRef(this));

            const onRemoved = this.onRemoved;
            this.onRemoved = function () {
                onRemoved?.apply(this, arguments);
            };
        };

        // Called after a workflow is loaded AND when ComfyUI switches workflow tabs.
        const onLoaded = nodeType.prototype.loadedGraphNode;
        nodeType.prototype.loadedGraphNode = function () {
            onLoaded?.apply(this, arguments);
            normalizeRenderTimeTitle(this);
            // graph.extra first (authoritative), then the per-tab WeakMap (survives
            // graph configure cycles when ComfyUI reloads from the saved file).
            const entry = _getEntryForNode(this);
            if (!entry) return;
            this._rtEntry = entry;
            const container = this.timingWidget?.element;
            if (!container) return;
            if (container.dataset.activeTab === "timing") {
                const body = container.querySelector(".tr-body");
                if (body) renderTimingBody(body, this._rtEntry);
            }
        };

        // Right-click context menu — Refresh action
        const origGetMenuOpts = nodeType.prototype.getExtraMenuOptions;
        nodeType.prototype.getExtraMenuOptions = function(canvas, options) {
            origGetMenuOpts?.apply(this, arguments);
            options.push(null);  // separator
            options.push({
                content: "↺ Refresh Timing Data",
                callback: () => {
                    const container = this.timingWidget?.element;
                    if (!container) return;
                    const entry = _getEntryForNode(this);
                    this._rtEntry = entry ?? null;
                    const body = container.querySelector(".tr-body");
                    if (body && container.dataset.activeTab === "timing") renderTimingBody(body, this._rtEntry);
                },
            });
        };
    },

    setup() {
        // Re-render all nodes when the browser tab becomes visible again.
        // Each node re-reads from its OWN graph so tabs never bleed into each other.
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState !== "visible") return;
            for (const ref of [..._nodeInstances]) {
                const node = ref.deref();
                if (!node) { _nodeInstances.delete(ref); continue; }
                _refreshNodeBody(node);
            }
        });

        // Receive backend push after each completed run
        api.addEventListener("render_time.update", (event) => {
            const { prompt_id, render_time_report, latest } = event.detail ?? {};

            // Inject into the graph that is currently active (the one that just ran)
            if (Array.isArray(render_time_report)) injectIntoGraph(render_time_report);

            const entry = latest ?? render_time_report?.at(-1) ?? null;

            // Persist against the active workflow tab — survives graph configure cycles
            const wf = _activeWorkflow();
            if (wf && Array.isArray(render_time_report)) _wfEntries.set(wf, render_time_report);
            else if (wf && entry) _wfEntries.set(wf, entry);

            // Only update nodes that belong to the graph that just ran.
            // node.graph === app.graph when the node is in the active workflow tab.
            // Nodes in other tabs have a different graph object and are left untouched.
            for (const ref of [..._nodeInstances]) {
                const node = ref.deref();
                if (!node) { _nodeInstances.delete(ref); continue; }
                if (node.graph !== app.graph) continue;   // ← different tab — skip
                node._rtEntry = Array.isArray(render_time_report)
                    ? _entryFromReportsForNode(render_time_report, node)
                    : entry;
                const container = node.timingWidget?.element;
                if (!container) continue;
                if (container.dataset.activeTab === "timing") {
                    const body = container.querySelector(".tr-body");
                    if (body) renderTimingBody(body, node._rtEntry);
                }
            }

            if (_notifyEnabled) playChime();
        });

        console.log("[Render Time] Extension loaded. Add the 'Render Time' node to your workflow.");
    },
});
