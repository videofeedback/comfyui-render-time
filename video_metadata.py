import json
import os
import copy
from pathlib import Path
from typing import Optional

from . import config_manager
from . import report_writer
from . import timing_store


def patch_save_image(plugin_tag: str) -> None:
    """Patch ComfyUI image save paths so the plugin can track real PNG outputs."""
    try:
        import nodes as comfy_nodes
        from comfy_execution.utils import get_executing_context
        from comfy_api.latest._ui import ImageSaveHelper
    except Exception as exc:
        print(f"{plugin_tag} Warning: could not patch Save Image: {exc}")
        return

    def _resolve_prompt_id() -> Optional[str]:
        prompt_id = None
        try:
            ctx = get_executing_context()
            if ctx is not None:
                prompt_id = ctx.prompt_id
        except Exception:
            pass
        return prompt_id or timing_store.get_active_prompt_id()

    def _build_saved_image_info(base_dir, item) -> Optional[dict]:
        if not isinstance(item, dict):
            return None

        filename = str(item.get("filename") or "").strip()
        if not filename or Path(filename).suffix.lower() != ".png":
            return None

        subfolder = str(item.get("subfolder") or "").strip()
        folder_type = str(item.get("type") or item.get("folder_type") or "output").strip() or "output"
        base_path = Path(str(base_dir))
        path = base_path / subfolder / filename if subfolder else base_path / filename

        return {
            "path": str(path),
            "filename": filename,
            "subfolder": subfolder,
            "folder_type": folder_type,
            "format": "png",
        }

    if not getattr(comfy_nodes.SaveImage, "_render_time_patched", False):
        original_core_save_images = comfy_nodes.SaveImage.save_images

        def _patched_core_save_images(self, images, filename_prefix="ComfyUI", prompt=None, extra_pnginfo=None):
            result = original_core_save_images(
                self,
                images,
                filename_prefix=filename_prefix,
                prompt=prompt,
                extra_pnginfo=extra_pnginfo,
            )

            prompt_id = _resolve_prompt_id()
            if prompt_id and getattr(self, "type", None) == "output":
                for item in ((result.get("ui") or {}).get("images") or []):
                    image_info = _build_saved_image_info(getattr(self, "output_dir", ""), item)
                    if image_info:
                        timing_store.add_saved_image(prompt_id, image_info)
            return result

        comfy_nodes.SaveImage.save_images = _patched_core_save_images
        comfy_nodes.SaveImage._render_time_patched = True

    if not getattr(ImageSaveHelper, "_render_time_patched", False):
        original_helper_save_images = ImageSaveHelper.save_images
        original_helper_save_animated_png = ImageSaveHelper.save_animated_png

        def _track_helper_results(prompt_id: Optional[str], results) -> None:
            if not prompt_id:
                return
            if isinstance(results, dict):
                iterable = [results]
            else:
                iterable = list(results or [])
            for item in iterable:
                image_info = _build_saved_image_info(report_writer._get_output_dir(), item)
                if image_info:
                    timing_store.add_saved_image(prompt_id, image_info)

        def _patched_helper_save_images(images, filename_prefix: str, folder_type, cls, compress_level=4):
            result = original_helper_save_images(
                images,
                filename_prefix=filename_prefix,
                folder_type=folder_type,
                cls=cls,
                compress_level=compress_level,
            )
            if str(getattr(folder_type, "value", folder_type)).lower() == "output":
                _track_helper_results(_resolve_prompt_id(), result)
            return result

        def _patched_helper_save_animated_png(
            images,
            filename_prefix: str,
            folder_type,
            cls,
            fps: float,
            compress_level: int,
        ):
            result = original_helper_save_animated_png(
                images,
                filename_prefix=filename_prefix,
                folder_type=folder_type,
                cls=cls,
                fps=fps,
                compress_level=compress_level,
            )
            if str(getattr(folder_type, "value", folder_type)).lower() == "output":
                _track_helper_results(_resolve_prompt_id(), [result])
            return result

        ImageSaveHelper.save_images = staticmethod(_patched_helper_save_images)
        ImageSaveHelper.save_animated_png = staticmethod(_patched_helper_save_animated_png)
        ImageSaveHelper._render_time_patched = True

    print(f"{plugin_tag} Image save pipeline patched for PNG preview tracking.")


def patch_save_video(plugin_tag: str) -> None:
    """Patch ComfyUI's low-level video save path so the plugin owns MP4 metadata."""
    try:
        import folder_paths
        from comfy_execution.utils import get_executing_context
        from comfy_api.latest._input_impl.video_types import (
            VideoCodec,
            VideoContainer,
            VideoFromComponents,
            VideoFromFile,
        )
    except Exception as exc:
        print(f"{plugin_tag} Warning: could not patch Save Video: {exc}")
        return

    if getattr(VideoFromFile, "_render_time_patched", False):
        return

    original_file_save_to = VideoFromFile.save_to
    original_components_save_to = VideoFromComponents.save_to

    def _tracked_video_info(path: str) -> Optional[dict]:
        try:
            path_obj = Path(path)
            if not path_obj.suffix:
                return None

            output_dir = Path(folder_paths.get_output_directory())
            subfolder = ""
            folder_type = "custom"
            try:
                parent_rel = path_obj.parent.relative_to(output_dir)
                subfolder = "" if str(parent_rel) == "." else parent_rel.as_posix()
                folder_type = "output"
            except ValueError:
                subfolder = ""

            return {
                "path": str(path_obj),
                "filename": path_obj.name,
                "subfolder": subfolder,
                "folder_type": folder_type,
                "format": path_obj.suffix.lstrip(".").lower(),
            }
        except Exception:
            return None

    def _resolve_save_video_prompt(path) -> tuple[Optional[str], bool]:
        if not isinstance(path, (str, os.PathLike)):
            return None, False

        prompt_id = None
        node_id = None
        try:
            ctx = get_executing_context()
            if ctx is not None:
                prompt_id = ctx.prompt_id
                node_id = ctx.node_id
        except Exception:
            pass

        if not prompt_id:
            prompt_id = timing_store.get_active_prompt_id()

        if not prompt_id:
            return None, False

        if node_id is None:
            return prompt_id, True

        record = timing_store.get_record(prompt_id) or {}
        prompt = record.get("api_prompt") or {}
        node_info = prompt.get(str(node_id)) or {}
        return prompt_id, node_info.get("class_type") == "SaveVideo"

    def _wrap_save_to(original_save_to):
        def _patched_save_to(self, path, format=None, codec=None, metadata=None):
            if format is None:
                format = VideoContainer.AUTO
            if codec is None:
                codec = VideoCodec.AUTO

            prompt_id, is_save_video = _resolve_save_video_prompt(path)
            if not is_save_video:
                return original_save_to(self, path, format=format, codec=codec, metadata=metadata)

            result = original_save_to(
                self,
                path,
                format=format,
                codec=codec,
                metadata=None,
            )

            if prompt_id:
                video_info = _tracked_video_info(os.fspath(path))
                if video_info:
                    timing_store.add_saved_video(prompt_id, video_info)
            return result

        return _patched_save_to

    VideoFromFile.save_to = _wrap_save_to(original_file_save_to)
    VideoFromComponents.save_to = _wrap_save_to(original_components_save_to)
    VideoFromFile._render_time_patched = True
    VideoFromComponents._render_time_patched = True
    print(f"{plugin_tag} Video save pipeline patched for MP4 metadata finalization.")


def finalize_saved_videos(prompt_id: str, timing_entry: dict, plugin_tag: str) -> dict:
    """Write final metadata to the source MP4 and a Render Time managed MP4 copy."""
    cfg = config_manager.get_config()
    if cfg.get("video_metadata_enabled", True) is False:
        return enrich_entry_with_media_preview(prompt_id, timing_entry)

    record = timing_store.get_record(prompt_id)
    if record is None:
        return enrich_entry_with_media_preview(prompt_id, timing_entry)

    source_video_infos = timing_store.get_saved_videos(prompt_id)
    valid_sources = [
        video_info for video_info in source_video_infos
        if Path(str(video_info.get("path") or "")).exists()
        and Path(str(video_info.get("path") or "")).suffix.lower() == ".mp4"
    ]
    if not valid_sources:
        return enrich_entry_with_media_preview(prompt_id, timing_entry)

    output_dir = report_writer._get_output_dir()
    managed_dir = report_writer._resolve_path(cfg.get("workflow_mp4", {}), output_dir)
    stem = report_writer.get_timed_output_stem(prompt_id, record, cfg=cfg)
    final_video_infos = []

    for index, video_info in enumerate(valid_sources, start=1):
        source_path = Path(str(video_info.get("path") or ""))
        try:
            source_entry = enrich_entry_with_media_preview(
                prompt_id,
                timing_entry,
                preview_video_info=video_info,
            )
            source_metadata = _build_final_video_metadata(prompt_id, source_entry)
            if source_metadata:
                _rewrite_video_metadata(source_path, source_path, source_metadata)
            print(f"{plugin_tag} Video metadata updated: {source_path.name}")

            managed_info = _build_managed_video_info(
                source_path,
                managed_dir,
                stem,
                index,
                len(valid_sources),
            )
            managed_entry = enrich_entry_with_media_preview(
                prompt_id,
                timing_entry,
                preview_video_info=managed_info,
            )
            managed_metadata = _build_final_video_metadata(prompt_id, managed_entry)
            if managed_metadata:
                _rewrite_video_metadata(source_path, Path(str(managed_info["path"])), managed_metadata)
            final_video_infos.append(managed_info)
            print(f"{plugin_tag} Render Time MP4 saved: {managed_info['filename']}")
        except Exception as exc:
            print(f"{plugin_tag} Warning: could not update video metadata for {source_path.name}: {exc}")

    if final_video_infos:
        timing_store.set_saved_videos(prompt_id, final_video_infos)

    return enrich_entry_with_media_preview(prompt_id, timing_entry)


def enrich_entry_with_media_preview(
    prompt_id: str,
    timing_entry: dict,
    preview_video_info: Optional[dict] = None,
    preview_image_info: Optional[dict] = None,
) -> dict:
    """Attach saved media preview metadata to a timing entry."""
    result = copy.deepcopy(timing_entry)

    image_infos = timing_store.get_saved_images(prompt_id)
    image_outputs = [
        v for v in (
            report_writer.build_image_preview_info(image_info)
            for image_info in image_infos
        )
        if v
    ]
    if image_outputs:
        result["image_outputs"] = image_outputs

    video_infos = timing_store.get_saved_videos(prompt_id)
    video_outputs = [
        v for v in (
            report_writer.build_video_preview_info(video_info)
            for video_info in video_infos
        )
        if v
    ]
    if video_outputs:
        result["video_outputs"] = video_outputs

    chosen_image = report_writer.build_image_preview_info(preview_image_info) if preview_image_info else None
    if chosen_image is None and image_outputs:
        chosen_image = image_outputs[-1]
    if chosen_image is None:
        chosen_image = _get_existing_preview(prompt_id, "preview_image")
    if chosen_image:
        result["preview_image"] = chosen_image
        if "image_outputs" not in result:
            result["image_outputs"] = [chosen_image]

    chosen_video = report_writer.build_video_preview_info(preview_video_info) if preview_video_info else None
    if chosen_video is None and video_outputs:
        chosen_video = video_outputs[-1]
    if chosen_video is None:
        chosen_video = _get_existing_preview(prompt_id, "preview_video")
    if chosen_video:
        result["preview_video"] = chosen_video
        if "video_outputs" not in result:
            result["video_outputs"] = [chosen_video]

    return result


def enrich_entry_with_video_preview(
    prompt_id: str,
    timing_entry: dict,
    preview_video_info: Optional[dict] = None,
) -> dict:
    """Backward-compatible wrapper for media preview enrichment."""
    return enrich_entry_with_media_preview(
        prompt_id,
        timing_entry,
        preview_video_info=preview_video_info,
    )


def _get_existing_preview(prompt_id: str, key: str) -> Optional[dict]:
    """Reuse a previously embedded preview when the current run did not write a new file."""
    record = timing_store.get_record(prompt_id) or {}
    workflow = record.get("workflow") or {}
    reports = ((workflow.get("extra") or {}).get("render_time_report")) or []
    if not reports:
        return None
    preview = reports[-1].get(key)
    if not isinstance(preview, dict):
        return None
    return copy.deepcopy(preview)


def _build_final_video_metadata(prompt_id: str, timing_entry: dict) -> Optional[dict]:
    record = timing_store.get_record(prompt_id)
    if record is None:
        return None

    metadata = {}
    extra_pnginfo = copy.deepcopy(record.get("extra_pnginfo") or {})
    for key, value in extra_pnginfo.items():
        if key in {"workflow", "timing_report"}:
            continue
        metadata[key] = value

    api_prompt = record.get("api_prompt")
    if api_prompt is not None:
        metadata["prompt"] = api_prompt

    metadata["workflow"] = report_writer.build_embedded_workflow(record, timing_entry)
    metadata["timing_report"] = timing_entry

    log_path = timing_store.get_live_log_path(prompt_id)
    if log_path:
        metadata["render_time_log_filename"] = Path(log_path).name
        log_text = _read_text_file(log_path)
        if log_text:
            metadata["render_time_log"] = log_text

    return metadata


def _build_managed_video_info(
    source_path: Path,
    output_dir: Path,
    stem: str,
    index: int,
    total: int,
) -> dict:
    """Return the managed Render Time MP4 descriptor for a saved video."""
    suffix = source_path.suffix.lower() or ".mp4"
    numbered = f"-{index:02d}" if total > 1 else ""
    target_path = output_dir / report_writer.build_output_filename(
        stem,
        f"VIDEO{numbered}",
        suffix.lstrip("."),
    )

    output_root = report_writer._get_output_dir()
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
        "format": suffix.lstrip("."),
    }


def _read_text_file(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return None


def _metadata_value(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _rewrite_video_metadata(source_path: Path, output_path: Path, metadata: dict) -> None:
    import av
    from av.subtitles.stream import SubtitleStream

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f"{output_path.stem}.__render_time__.tmp{output_path.suffix}")
    if temp_path.exists():
        temp_path.unlink()

    try:
        with av.open(str(source_path), mode="r") as input_container:
            with av.open(
                str(temp_path),
                mode="w",
                options={"movflags": "use_metadata_tags"},
            ) as output_container:
                for key, value in input_container.metadata.items():
                    if key not in metadata:
                        output_container.metadata[key] = value

                for key, value in metadata.items():
                    output_container.metadata[key] = _metadata_value(value)

                stream_map = {}
                for stream in input_container.streams:
                    if isinstance(stream, (av.VideoStream, av.AudioStream, SubtitleStream)):
                        out_stream = output_container.add_stream_from_template(
                            template=stream,
                            opaque=True,
                        )
                        stream_map[stream] = out_stream

                for packet in input_container.demux():
                    if packet.stream in stream_map and packet.dts is not None:
                        packet.stream = stream_map[packet.stream]
                        output_container.mux(packet)

        os.replace(temp_path, output_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
