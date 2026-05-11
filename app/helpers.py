"""
app/helpers.py — Shared utilities dùng trong nhiều routes.

  build_state_payload()  — tạo response dict chuẩn từ lyra_ai state
  build_stream_context() — tổng hợp stream context string để inject vào prompt
  sanitize_input()       — làm sạch input từ user
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core import MiniAI
    from viewer_tracker import ViewerTracker, ChatPatternAnalyzer


# ------------------------------------------------------------------ #
# Response builder                                                     #
# ------------------------------------------------------------------ #

def build_state_payload(ai: "MiniAI", result: dict | None = None) -> dict:
    """
    Tạo response dict chuẩn gửi về frontend / SSE.
    result chứa: reply, monologue, emotion, action, mood, affection,
                 time_period, time_gap_hours, vad
    """
    r = result or {}
    return {
        "affection":             int(round(ai.affection)),
        "mood":                  int(round(ai.mood)),
        "emotion":               r.get("emotion") or ai.emotion_from_state(),
        "action":                r.get("action") or "NONE",
        "monologue":             r.get("monologue") or "",
        "reply":                 r.get("reply") or "",
        "rolling_stream_summary": ai.memory._rolling_stream_summary or "",
        "time_period":           r.get("time_period") or getattr(ai, "time_period", "afternoon"),
        "time_gap_hours":        (
            r.get("time_gap_hours")
            if r.get("time_gap_hours") is not None
            else getattr(ai, "time_gap_hours", None)
        ),
    }


# ------------------------------------------------------------------ #
# Stream context builder                                               #
# ------------------------------------------------------------------ #

def build_stream_context(
    lyra_ai:        "MiniAI",
    viewer_tracker: "ViewerTracker",
    chat_analyzer:  "ChatPatternAnalyzer",
    sender_id:      str,
    sender_name:    str,
    platform:       str,
    channel_id:     str,
    viewer_info:    dict,
) -> str:
    """
    Tổng hợp stream context string từ nhiều nguồn:
      viewer context + style hints + stream content context
    """
    # Viewer context từ tracker
    ctx = viewer_tracker.get_stream_context(
        sender_id, sender_name, platform, channel_id, viewer_info
    )

    # Chat style hints
    style_hints = chat_analyzer.get_style_hints(channel_id, platform)
    if style_hints:
        ctx = f"{ctx}\n{style_hints}" if ctx else style_hints

    # Stream content context (title, game, milestones)
    content_ctx = _build_stream_content_context(lyra_ai)
    if content_ctx:
        ctx = f"{content_ctx}\n{ctx}" if ctx else content_ctx

    return ctx


def _build_stream_content_context(lyra_ai: "MiniAI") -> str:
    """Tạo [STREAM CONTEXT] block inject vào prompt."""
    lines = ["[STREAM CONTEXT]"]
    lines.append("Hôm nay Lyra stream chuyện phiếm, tâm sự với mọi người.")
    try:
        milestones = lyra_ai.memory.get_stream_milestones(limit=3)
        if milestones:
            strs = [f"{m['description']} ({m['achieved_at'][:10]})" for m in milestones]
            lines.append(f"Kỷ niệm stream: {' | '.join(strs)}")
    except Exception:
        pass
    lines.append("[/STREAM CONTEXT]")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Input sanitization                                                   #
# ------------------------------------------------------------------ #

def sanitize_input(text: str, max_length: int = 1000) -> str:
    """
    Làm sạch input:
      - Loại control characters (chống prompt injection)
      - Loại ký tự HTML/script nguy hiểm
      - Giữ lại dấu nháy đơn (apostrophe) vì tiếng Việt dùng bình thường
      - Cắt theo max_length
    """
    if not text or not isinstance(text, str):
        return ""
    text = "".join(ch for ch in text if ord(ch) >= 32)
    # Chỉ strip các ký tự thực sự nguy hiểm cho HTML/injection
    # Bỏ dấu nháy đơn ' khỏi blacklist — tiếng Việt cần giữ lại
    text = re.sub(r'[<>\"%;)(&+]', "", text)
    return text.strip()[:max_length]
