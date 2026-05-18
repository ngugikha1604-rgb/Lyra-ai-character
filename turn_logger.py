"""
turn_logger.py - Structured per-turn JSON logger.

Moi turn ghi 1 dong JSON vao logs/turns.jsonl.
Format tuong thich voi jq, pandas, va bat ky tool parse JSONL nao.

Usage (trong chat route):
    from turn_logger import log_turn
    log_turn(user_input, result, lyra_ai, latency_ms=...)
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

_LOG_DIR  = os.path.join(os.path.dirname(__file__), "logs")
_LOG_PATH = os.path.join(_LOG_DIR, "turns.jsonl")
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB - rotate khi vuot qua

_lock = threading.Lock()


def _ensure_dir() -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)


def _rotate_if_needed() -> None:
    """Doi ten file cu thanh turns.jsonl.bak khi > MAX_BYTES."""
    if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > _MAX_BYTES:
        bak = _LOG_PATH + ".bak"
        if os.path.exists(bak):
            os.remove(bak)
        os.rename(_LOG_PATH, bak)


def log_turn(
    user_input: str,
    result: dict | None,
    lyra_ai,
    latency_ms: float | None = None,
    source_type: str = "owner",
) -> None:
    """
    Ghi 1 dong JSON vao logs/turns.jsonl.

    Args:
        user_input  : tin nhan nguoi dung (truoc khi sanitize)
        result      : dict tra ve tu lyra_ai.chat() - co the None neu loi
        lyra_ai     : instance MiniAI de lay emotion state
        latency_ms  : thoi gian xu ly tinh bang ms
        source_type : "owner" | "viewer" | "youtube"
    """
    try:
        _ensure_dir()
        result = result or {}
        emotion = getattr(lyra_ai, "emotion", None)

        record = {
            "ts":           datetime.utcnow().isoformat() + "Z",
            "source":       source_type,
            # Input
            "user_len":     len(user_input),
            "user_preview": user_input[:60],
            # Output
            "reply_len":    len(result.get("reply") or ""),
            "emotion":      result.get("emotion", ""),
            "action":       result.get("action", ""),
            # Latency
            "latency_ms":   round(latency_ms, 1) if latency_ms is not None else None,
            # Emotion state snapshot
            "mood":         round(getattr(emotion, "mood", 0), 1)         if emotion else None,
            "attention":    round(getattr(emotion, "attention", 5), 1)    if emotion else None,
            "affection":    round(getattr(emotion, "affection", 50), 1)   if emotion else None,
            "dominance":    round(getattr(emotion, "dominance", 0.5), 2)  if emotion else None,
            "irritability": round(getattr(emotion, "irritability", 0), 2) if emotion else None,
            # Metadata
            "turn":         getattr(lyra_ai, "turn_counter", None),
            "model_used":   result.get("model_used", ""),
        }

        line = json.dumps(record, ensure_ascii=False)

        with _lock:
            _rotate_if_needed()
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    except Exception as e:
        # Logger khong duoc crash app chinh
        print(f"[TurnLogger] Warning: could not write log - {e}")
