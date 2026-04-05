"""API routes for marker detection debugging and preview.

Provides endpoints to run detection analysis on individual media items
and return raw detection data (black frames, silence regions, combined
result) without writing markers to the Plex database.
"""

import os

from flask import jsonify, request
from loguru import logger

from ..auth import api_token_required
from . import api


@api.route("/detection/analyze", methods=["POST"])
@api_token_required
def analyze_detection():
    """Run credits detection on a media item and return raw results.

    Does NOT write markers — purely diagnostic.  Returns the detected
    black frames, silence regions, and the combined credits segment
    so the user can visualize why detection did or didn't work.

    Request JSON:
        item_key: Plex metadata key (e.g. "/library/metadata/12345")

    Returns JSON with: duration, black_frames, silence_regions,
    credits_segment, and detection parameters used.
    """
    data = request.get_json() or {}
    item_key = data.get("item_key", "").strip()

    if not item_key:
        return jsonify({"error": "item_key is required"}), 400

    try:
        from ...config import load_config
        from ...credits_detection import (
            CreditsDetectionConfig,
            _run_blackdetect,
            _run_silencedetect,
            _combine_detections,
        )
        from ...plex_client import plex_server, retry_plex_call
        from ..settings_manager import get_settings_manager

        config = load_config()
        settings = get_settings_manager()

        if settings.plex_url:
            config.plex_url = settings.plex_url
        if settings.plex_token:
            config.plex_token = settings.plex_token
        if settings.plex_config_folder:
            config.plex_config_folder = settings.plex_config_folder

        from ...config import normalize_path_mappings

        path_mappings = normalize_path_mappings(settings)
        if path_mappings:
            config.path_mappings = path_mappings

        plex = plex_server(config)

        # Resolve file path
        tree_data = retry_plex_call(plex.query, f"{item_key}/tree")
        media_part = tree_data.find(".//MediaPart")
        if media_part is None:
            return jsonify({"error": "No media parts found for this item"}), 404

        plex_path = media_part.attrib.get("file", "")
        mappings = getattr(config, "path_mappings", None) or []
        if mappings:
            from ...media_processing import plex_path_to_local, sanitize_path

            media_file = sanitize_path(plex_path_to_local(plex_path, mappings))
        else:
            from ...media_processing import sanitize_path

            media_file = sanitize_path(plex_path)

        if not os.path.isfile(media_file):
            return jsonify({"error": f"File not found: {media_file}"}), 404

        # Get duration
        from pymediainfo import MediaInfo

        mi = MediaInfo.parse(media_file)
        duration_ms = 0
        for track in mi.video_tracks:
            if track.duration:
                duration_ms = float(track.duration)
                break
        if not duration_ms:
            for track in mi.general_tracks:
                if track.duration:
                    duration_ms = float(track.duration)
                    break
        if not duration_ms:
            return jsonify({"error": "Cannot determine video duration"}), 400

        total_duration_sec = duration_ms / 1000.0

        # Build detection config from settings
        scan_last_pct = float(settings.get("credits_scan_last_pct", 25.0))
        min_duration = float(settings.get("credits_min_duration", 15.0))
        det_config = CreditsDetectionConfig(
            scan_last_pct=scan_last_pct,
            min_credits_duration=min_duration,
        )

        seek_to = total_duration_sec * (1.0 - det_config.scan_last_pct / 100.0)

        # Run detection
        black_frames = _run_blackdetect(
            media_file,
            seek_to,
            config.ffmpeg_path,
            black_min_duration=det_config.black_min_duration,
            pix_threshold=det_config.black_pix_threshold,
        )

        silence_regions = _run_silencedetect(
            media_file,
            seek_to,
            config.ffmpeg_path,
            noise_threshold=det_config.silence_noise_threshold,
            silence_duration=det_config.silence_min_duration,
        )

        segment = _combine_detections(
            black_frames,
            silence_regions,
            total_duration_sec,
            min_credits_duration_sec=det_config.min_credits_duration,
            max_credits_start_pct=det_config.max_credits_start_pct,
        )

        # Get existing markers
        rating_key = int(item_key.rstrip("/").split("/")[-1])
        existing_markers = []
        try:
            item = retry_plex_call(plex.fetchItem, rating_key)
            for marker in getattr(item, "markers", []) or []:
                existing_markers.append(
                    {
                        "type": getattr(marker, "type", ""),
                        "start_ms": getattr(marker, "start", 0),
                        "end_ms": getattr(marker, "end", 0),
                    }
                )
        except Exception:
            pass

        # Get item title for display
        title = ""
        try:
            video_el = tree_data.find(".//Video")
            if video_el is not None:
                title = video_el.attrib.get("title", "")
                gp_title = video_el.attrib.get("grandparentTitle", "")
                if gp_title:
                    se = video_el.attrib.get("parentIndex", "")
                    ep = video_el.attrib.get("index", "")
                    title = f"{gp_title} S{se}E{ep} — {title}"
        except Exception:
            pass

        return jsonify(
            {
                "title": title,
                "file": media_file,
                "duration_sec": total_duration_sec,
                "scan_start_sec": seek_to,
                "config": {
                    "scan_last_pct": det_config.scan_last_pct,
                    "min_credits_duration": det_config.min_credits_duration,
                    "black_min_duration": det_config.black_min_duration,
                    "black_pix_threshold": det_config.black_pix_threshold,
                    "silence_noise_threshold": det_config.silence_noise_threshold,
                    "silence_min_duration": det_config.silence_min_duration,
                    "max_credits_start_pct": det_config.max_credits_start_pct,
                },
                "black_frames": [
                    {
                        "start": bf.start,
                        "end": bf.end,
                        "duration": bf.duration,
                    }
                    for bf in black_frames
                ],
                "silence_regions": [
                    {
                        "start": sr.start,
                        "end": sr.end,
                        "duration": sr.duration,
                    }
                    for sr in silence_regions
                ],
                "credits_segment": (
                    {
                        "start_ms": segment.start_ms,
                        "end_ms": segment.end_ms,
                        "confidence": segment.confidence,
                        "method": segment.method,
                    }
                    if segment
                    else None
                ),
                "existing_markers": existing_markers,
            }
        )

    except Exception as exc:
        logger.error(f"Detection analysis failed: {exc}")
        return jsonify({"error": str(exc)}), 500
