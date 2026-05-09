"""
routes/stream.py — Stream control routes.

  POST /stream/start           — bắt đầu stream, khởi động YouTube poller
  POST /stream/stop            — dừng stream
  GET  /stream/status          — trạng thái stream hiện tại
  GET  /stream/analytics       — viewer stats + queue snapshot
  GET  /stream/debug/queue     — debug priority queues (admin)
  POST /stream-chat            — nhận chat event thủ công qua HTTP (không qua YouTube poller)
  GET  /viewers                — top viewers
"""

from __future__ import annotations

import os
import traceback

from flask import Blueprint, jsonify, request, current_app

from app.helpers import build_state_payload, sanitize_input
from live_context import (
    set_stream_active,
    reset_live_context,
    load_live_context,
    record_donation,
    record_regular_arrival,
)

bp = Blueprint("stream", __name__)


# ------------------------------------------------------------------ #
# POST /stream/start                                                   #
# ------------------------------------------------------------------ #

@bp.route("/stream/start", methods=["POST"])
def stream_start():
    lyra_ai        = current_app.lyra_ai
    yt_poller      = current_app.yt_poller
    stream_service = current_app.stream_service

    try:
        data       = request.get_json() or {}
        video_id   = data.get("video_id", os.environ.get("YOUTUBE_VIDEO_ID", ""))
        chat_id    = data.get("chat_id",  os.environ.get("YOUTUBE_LIVE_CHAT_ID", ""))
        credentials = current_app.yt_credentials  # set bởi auth route

        if not chat_id and video_id and credentials:
            from youtube_chat import get_live_chat_id
            chat_id = get_live_chat_id(video_id, credentials)

        if not chat_id:
            return jsonify({"error": "chat_id required (or provide video_id + OAuth)"}), 400

        yt_poller.start(chat_id, credentials)
        lyra_ai.is_streaming = True
        stream_service.reset_greeted_set()
        set_stream_active(True)

        print(f"[Stream] Started — chat_id={chat_id}")
        return jsonify({"ok": True, "chat_id": chat_id})

    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Failed to start stream"}), 500


# ------------------------------------------------------------------ #
# POST /stream/stop                                                    #
# ------------------------------------------------------------------ #

@bp.route("/stream/stop", methods=["POST"])
def stream_stop():
    lyra_ai   = current_app.lyra_ai
    yt_poller = current_app.yt_poller

    yt_poller.stop()
    lyra_ai.is_streaming = False
    set_stream_active(False)
    print("[Stream] Stopped.")
    return jsonify({"ok": True})


# ------------------------------------------------------------------ #
# GET /stream/status                                                   #
# ------------------------------------------------------------------ #

@bp.route("/stream/status")
def stream_status():
    lyra_ai   = current_app.lyra_ai
    yt_poller = current_app.yt_poller
    return jsonify({
        "is_streaming": lyra_ai.is_streaming,
        "poller_running": yt_poller._is_running,
        "live_context":  load_live_context(),
    })


# ------------------------------------------------------------------ #
# GET /stream/analytics                                               #
# ------------------------------------------------------------------ #

@bp.route("/stream/analytics")
def stream_analytics():
    viewer_tracker  = current_app.viewer_tracker
    stream_service  = current_app.stream_service

    top = viewer_tracker.get_top_viewers(limit=20)
    return jsonify({
        "top_viewers":    top,
        "queue_snapshot": stream_service.get_queue_snapshot(),
    })


# ------------------------------------------------------------------ #
# GET /stream/debug/queue  (admin)                                    #
# ------------------------------------------------------------------ #

@bp.route("/stream/debug/queue")
def debug_queue():
    from app.middleware import require_auth
    @require_auth
    def _inner():
        return jsonify(current_app.stream_service.get_queue_snapshot())
    return _inner()


# ------------------------------------------------------------------ #
# POST /stream-chat  (manual injection)                               #
# ------------------------------------------------------------------ #

@bp.route("/stream-chat", methods=["POST"])
def stream_chat():
    """
    Nhận chat event thủ công (không qua YouTube poller).
    Dùng cho: test, Discord bridge, custom platform webhook.
    """
    lyra_ai        = current_app.lyra_ai
    viewer_tracker = current_app.viewer_tracker
    chat_analyzer  = current_app.chat_analyzer
    ai_chat_lock   = current_app.ai_chat_lock
    vts_bridge     = current_app.vts_bridge
    stream_service = current_app.stream_service

    try:
        data = request.get_json()
        if not data or "message" not in data or "sender_id" not in data:
            return jsonify({"error": "Missing required fields: message, sender_id"}), 400

        message     = sanitize_input(data["message"], max_length=1000)
        sender_id   = str(data["sender_id"]).strip()
        sender_name = str(data.get("sender_name", "Viewer")).strip()
        channel_id  = str(data.get("channel_id", "default")).strip()
        platform    = str(data.get("platform", "unknown")).strip()
        role        = str(data.get("role", "viewer")).strip()

        if not message or not sender_id:
            return jsonify({"error": "Empty message or sender_id"}), 400

        # Đẩy vào priority queue — xử lý async bởi consumer loop
        stream_service.enqueue_event({
            "message":     message,
            "sender_id":   sender_id,
            "sender_name": sender_name,
            "channel_id":  channel_id,
            "platform":    platform,
            "role":        role,
            "is_donor":    data.get("is_donor", False),
            "donate_amount": data.get("donate_amount", ""),
            "gender":      data.get("gender", "male"),
        })

        return jsonify({"ok": True, "queued": True})

    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Internal server error"}), 500


# ------------------------------------------------------------------ #
# GET /viewers                                                         #
# ------------------------------------------------------------------ #

@bp.route("/viewers")
def get_viewers():
    platform   = request.args.get("platform")
    channel_id = request.args.get("channel_id")
    limit      = min(int(request.args.get("limit", 10)), 50)
    top = current_app.viewer_tracker.get_top_viewers(
        platform=platform, channel_id=channel_id, limit=limit
    )
    return jsonify({"viewers": top, "count": len(top)})
