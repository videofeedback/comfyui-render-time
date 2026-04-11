# ComfyUI Render Time — __init__.py
# Author: Ramiro Montes De Oca
# GitHub: https://github.com/videofeedback/comfyui-render-time
#
# Native ComfyUI plugin that instruments the execution engine to capture
# per-node timing, machine configuration, and node settings, then writes
# reports to disk and embeds timing data into PNG metadata and workflow JSON.

import json
import time

import nodes as comfy_nodes
import execution as comfy_execution
from execution import PromptExecutor
from server import PromptServer
from aiohttp import web

from . import timing_store
from . import report_writer
from . import config_manager
from . import live_logger

_PLUGIN_TAG = "[render-time]"


# ─── Patch 1: get_output_data — per-node wall-clock timing ──────────────────

_orig_get_output_data = comfy_execution.get_output_data


async def _timed_get_output_data(prompt_id, unique_id, obj, input_data_all, **kwargs):
    node_type = type(obj).__name__
    timing_store.node_start(prompt_id, unique_id, node_type)
    live_logger.log_node_start(prompt_id, unique_id, node_type)
    _node_error = None
    try:
        result = await _orig_get_output_data(
            prompt_id, unique_id, obj, input_data_all, **kwargs
        )
    except Exception as _exc:
        _node_error = _exc
        live_logger.log_node_error(prompt_id, unique_id, node_type, str(_exc))
        raise
    finally:
        timing_store.node_end(prompt_id, unique_id)
        if _node_error is None:
            live_logger.log_node_end(
                prompt_id, unique_id, node_type,
                timing_store.get_node_duration(prompt_id, unique_id),
            )
    return result


comfy_execution.get_output_data = _timed_get_output_data


# ─── Patch 2: execute — cached-node tracking + PNG injection ────────────────

_orig_execute = comfy_execution.execute


async def _patched_execute(
    server, dynprompt, caches, current_item,
    extra_data, executed, prompt_id,
    execution_list, pending_subgraph_results, pending_async_nodes, ui_outputs
):
    unique_id = current_item

    # Check cache before calling the real execute (cache read is side-effect-free)
    cached = await caches.outputs.get(unique_id)
    if cached is not None:
        # Node will be skipped — record it as cached
        node_info = dynprompt.get_node(unique_id) or {}
        class_type = node_info.get("class_type", "unknown")
        timing_store.node_cached(prompt_id, unique_id, class_type)
        live_logger.log_node_cached(prompt_id, unique_id, class_type)
    else:
        # Check if this is an output/save node → inject timing snapshot into extra_pnginfo
        node_info = dynprompt.get_node(unique_id) or {}
        class_type = node_info.get("class_type", "")
        class_def = comfy_nodes.NODE_CLASS_MAPPINGS.get(class_type)
        if class_def and getattr(class_def, "OUTPUT_NODE", False):
            snapshot = timing_store.get_snapshot(prompt_id)
            if snapshot:
                if "extra_pnginfo" not in extra_data or extra_data["extra_pnginfo"] is None:
                    extra_data["extra_pnginfo"] = {}
                extra_data["extra_pnginfo"]["timing_report"] = json.dumps(snapshot)

    return await _orig_execute(
        server, dynprompt, caches, current_item,
        extra_data, executed, prompt_id,
        execution_list, pending_subgraph_results, pending_async_nodes, ui_outputs
    )


comfy_execution.execute = _patched_execute


# ─── Patch 3: PromptExecutor.execute_async — total timing + report trigger ──

_orig_execute_async = PromptExecutor.execute_async


async def _timed_execute_async(
    self, prompt, prompt_id, extra_data={}, execute_outputs=[]
):
    # Extract visual-editor workflow JSON (present when submitted from the UI)
    workflow = None
    try:
        epng = extra_data.get("extra_pnginfo") or {}
        workflow = epng.get("workflow")
    except Exception:
        pass

    timing_store.prompt_start(prompt_id, prompt, workflow)

    # Derive workflow name using the same helper as report_writer
    try:
        _record_now = timing_store.get_record(prompt_id) or {}
        _wf_name_for_log = report_writer._workflow_name(_record_now, prompt_id)
    except Exception:
        _wf_name_for_log = f"workflow_{prompt_id[:8]}"

    live_logger.open_log(
        prompt_id,
        workflow,
        timing_store.get_record(prompt_id)["wall_start"],
        _wf_name_for_log,
    )

    try:
        await _orig_execute_async(self, prompt, prompt_id, extra_data, execute_outputs)
    finally:
        timing_store.prompt_end(prompt_id)

        # Close the live log BEFORE writing any other output files
        _total_sec = (timing_store.get_record(prompt_id) or {}).get("total_sec") or 0.0
        live_logger.close_log(prompt_id, _total_sec)

        # Generate reports; get back the compact entry for workflow embedding
        try:
            entry = report_writer.generate(prompt_id)
        except Exception as exc:
            print(f"{_PLUGIN_TAG} Error generating report: {exc}")
            entry = None

        # Push timing update to the browser (broadcast — no client_id guard)
        # so the JS node can render and inject data into graph.extra for Ctrl+S
        if entry:
            try:
                # Always only the current run — never accumulate past entries
                updated_reports = [entry]

                self.server.send_sync(
                    "render_time.update",
                    {
                        "prompt_id": prompt_id,
                        "render_time_report": updated_reports,
                        "latest": entry,
                    },
                )
            except Exception as exc:
                print(f"{_PLUGIN_TAG} Error sending WS update: {exc}")


PromptExecutor.execute_async = _timed_execute_async


# ─── HTTP routes ─────────────────────────────────────────────────────────────

async def _route_latest(request):
    data = timing_store.get_latest_snapshot()
    if data is None:
        return web.json_response({"error": "No completed run yet"}, status=404)
    return web.json_response(data)


async def _route_by_id(request):
    prompt_id = request.match_info["prompt_id"]
    data = timing_store.get_snapshot(prompt_id)
    if data is None:
        return web.json_response({"error": "Prompt ID not found"}, status=404)
    return web.json_response(data)


async def _route_save_workflow(request):
    """Called by the JS node to save the timed workflow JSON to disk."""
    try:
        body = await request.json()
        prompt_id = body.get("prompt_id")
        workflow_name = body.get("workflow_name", "workflow")

        if not prompt_id:
            return web.json_response({"error": "prompt_id required"}, status=400)

        record = timing_store.get_record(prompt_id)
        if record is None:
            return web.json_response({"error": "prompt_id not found"}, status=404)

        total_sec = record.get("total_sec") or 0.0
        rows = report_writer._build_node_rows(record, prompt_id)
        timing_entry = report_writer.build_timing_report_entry(
            prompt_id, record, rows, total_sec
        )

        result = report_writer.save_timed_workflow(prompt_id, workflow_name, timing_entry)
        return web.json_response(result)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def _route_config_get(request):
    """Return current plugin configuration."""
    try:
        cfg = config_manager.get_config()
        cfg["_defaults"] = config_manager.get_default_descriptions()
        return web.json_response(cfg)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def _route_config_post(request):
    """Save plugin configuration."""
    try:
        body = await request.json()
        body.pop("_defaults", None)

        # Author/contact go to author.txt, not config.json
        author  = body.pop("workflow_author",  None)
        contact = body.pop("workflow_contact", None)
        if author is not None or contact is not None:
            existing = config_manager.get_author_info()
            config_manager.save_author_info(
                author  if author  is not None else existing["workflow_author"],
                contact if contact is not None else existing["workflow_contact"],
            )

        config_manager.save_config(body)
        print(f"{_PLUGIN_TAG} Config saved.")
        return web.json_response({"saved": True})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def _route_config_property(request):
    """Save a single config key — called by the Properties Panel onPropertyChanged."""
    try:
        body = await request.json()
        key   = body.get("key")
        value = body.get("value")
        if key is None:
            return web.json_response({"error": "key required"}, status=400)

        # Author/contact are stored in author.txt, never in config.json
        if key == "workflow_author":
            existing = config_manager.get_author_info()
            config_manager.save_author_info(str(value), existing["workflow_contact"])
            return web.json_response({"saved": True})
        if key == "workflow_contact":
            existing = config_manager.get_author_info()
            config_manager.save_author_info(existing["workflow_author"], str(value))
            return web.json_response({"saved": True})

        cfg = config_manager.get_config()
        # Handle nested output keys: toggle their enabled flag
        if key in ("embed_json", "txt_report", "isolated_json", "workflow_png"):
            cfg[key]["enabled"] = bool(value)
        else:
            cfg[key] = value
        config_manager.save_config(cfg)
        return web.json_response({"saved": True})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def _route_latest_full(request):
    """Return the full timing entry (same shape as render_time.update) for the last completed run."""
    if timing_store._latest_prompt_id is None:
        return web.json_response({"error": "No completed run yet"}, status=404)
    prompt_id = timing_store._latest_prompt_id
    record = timing_store.get_record(prompt_id)
    if record is None or not record.get("completed"):
        return web.json_response({"error": "No completed run yet"}, status=404)
    try:
        total_sec = record.get("total_sec") or 0.0
        rows = report_writer._build_node_rows(record, prompt_id)
        entry = report_writer.build_timing_report_entry(prompt_id, record, rows, total_sec)
        return web.json_response(entry)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


def _register_routes():
    try:
        app = PromptServer.instance.app
        # Static/specific routes MUST be registered before the dynamic {prompt_id}
        # wildcard, otherwise aiohttp matches "config" and "latest" as prompt IDs.
        app.router.add_get("/render-time/latest",           _route_latest)
        app.router.add_get("/render-time/latest/full",      _route_latest_full)
        app.router.add_get("/render-time/config",           _route_config_get)
        app.router.add_post("/render-time/config",          _route_config_post)
        app.router.add_post("/render-time/config/property", _route_config_property)
        app.router.add_post("/render-time/save-workflow",   _route_save_workflow)
        app.router.add_get("/render-time/{prompt_id}",      _route_by_id)   # wildcard last
        print(f"{_PLUGIN_TAG} Routes registered: /render-time/...")
    except Exception as exc:
        print(f"{_PLUGIN_TAG} Warning: could not register routes: {exc}")


_register_routes()

print(f"{_PLUGIN_TAG} Execution engine patched. Ready.")


# ─── ComfyUI node class ──────────────────────────────────────────────────────

class RenderTimeNode:
    """
    Standalone display node — no connections required.
    Add it to any workflow; it shows per-run timing data in its body
    and updates automatically when a run completes.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {"optional": {}}

    RETURN_TYPES = ()
    OUTPUT_NODE = False
    FUNCTION = "noop"
    CATEGORY = ""
    DESCRIPTION = "Displays per-run execution timing and metadata. No connections needed."

    def noop(self):
        return {}


# ─── ComfyUI plugin exports ───────────────────────────────────────────────────

NODE_CLASS_MAPPINGS        = {"RenderTime": RenderTimeNode}
NODE_DISPLAY_NAME_MAPPINGS = {"RenderTime": "📝 Render Time"}
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
