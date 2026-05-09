"""
routes/stream_events.py — SSE endpoint.

  GET /stream/events  — SSE event stream tới frontend (Live2D overlay, chat UI)
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app

bp = Blueprint("stream_events", __name__)


@bp.route("/stream/events")
def stream_events():
    """
    Server-Sent Events endpoint.
    Frontend kết nối một lần và nhận push events liên tục.
    """
    sse = current_app.sse_service

    return Response(
        sse.event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # Tắt Nginx buffering nếu có proxy
        },
    )
