"""
routes/health.py — Health check endpoint.

  GET /health  — kiểm tra trạng thái Ollama, 9router, VTS và DSPy brain.
  Trả về HTTP 200 nếu tất cả OK, 207 nếu có thành phần degraded.
"""

from __future__ import annotations

import time
import requests
from flask import Blueprint, jsonify, current_app

from config import (
    LIGHT_BASE_URL, ROUTER9_BASE_URL, ROUTER9_API_KEY,
)

bp = Blueprint("health", __name__)

_TIMEOUT = 3.0  # seconds per check — không để lâu hơn vì health check phải fast


def _check_ollama() -> dict:
    """Ping Ollama API (GET /api/tags)."""
    if not LIGHT_BASE_URL:
        return {"status": "disabled", "latency_ms": None}
    # LIGHT_BASE_URL thường là http://localhost:11434/api/chat — lấy base
    base = LIGHT_BASE_URL.split("/api/")[0] if "/api/" in LIGHT_BASE_URL else LIGHT_BASE_URL
    try:
        t0 = time.perf_counter()
        r = requests.get(f"{base}/api/tags", timeout=_TIMEOUT)
        ms = round((time.perf_counter() - t0) * 1000)
        if r.status_code == 200:
            models = [m.get("name", "") for m in r.json().get("models", [])]
            return {"status": "ok", "latency_ms": ms, "models": models}
        return {"status": "error", "latency_ms": ms, "http": r.status_code}
    except requests.exceptions.ConnectionError:
        return {"status": "down", "latency_ms": None, "error": "Connection refused"}
    except requests.exceptions.Timeout:
        return {"status": "timeout", "latency_ms": int(_TIMEOUT * 1000)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_router9() -> dict:
    """Ping 9router /health hoặc /models."""
    if not ROUTER9_BASE_URL:
        return {"status": "disabled", "latency_ms": None}
    headers = {}
    if ROUTER9_API_KEY:
        headers["Authorization"] = f"Bearer {ROUTER9_API_KEY}"
    # Thử /health trước, fallback /models
    for path in ("/health", "/models"):
        try:
            t0 = time.perf_counter()
            r = requests.get(f"{ROUTER9_BASE_URL}{path}", headers=headers, timeout=_TIMEOUT)
            ms = round((time.perf_counter() - t0) * 1000)
            if r.status_code == 200:
                return {"status": "ok", "latency_ms": ms, "endpoint": path}
            if r.status_code == 404:
                continue  # thử path tiếp theo
            return {"status": "error", "latency_ms": ms, "http": r.status_code}
        except requests.exceptions.ConnectionError:
            return {"status": "down", "latency_ms": None, "error": "Connection refused"}
        except requests.exceptions.Timeout:
            return {"status": "timeout", "latency_ms": int(_TIMEOUT * 1000)}
        except Exception as e:
            return {"status": "error", "error": str(e)}
    return {"status": "unknown", "note": "No standard health path found"}


def _check_vts() -> dict:
    """Kiểm tra VTube Studio bridge đã connect chưa."""
    try:
        bridge = current_app.vts_bridge
        connected = getattr(bridge, "_connected", False) or getattr(bridge, "connected", False)
        return {"status": "ok" if connected else "disconnected"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_dspy() -> dict:
    """Kiểm tra DSPy brain có load được không."""
    try:
        ai = current_app.lyra_ai
        brain = getattr(ai, "brain", None)
        if brain is None:
            return {"status": "not_loaded"}
        return {"status": "ok", "type": type(brain).__name__}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@bp.route("/health", methods=["GET"])
def health():
    """
    Kiểm tra toàn bộ dependencies. Trả về:
    - 200 nếu tất cả ok / disabled
    - 207 nếu có thành phần degraded (vẫn hoạt động được)
    - 503 nếu có thành phần down/timeout quan trọng
    """
    checks = {
        "ollama":  _check_ollama(),
        "router9": _check_router9(),
        "vts":     _check_vts(),
        "dspy":    _check_dspy(),
    }

    # Lyra vẫn hoạt động nếu ít nhất router9 OK (Groq qua 9router là primary)
    router9_ok = checks["router9"]["status"] == "ok"
    ollama_ok  = checks["ollama"]["status"] in ("ok", "disabled")

    # Xác định HTTP status code
    critical_down = checks["router9"]["status"] in ("down", "timeout") and \
                    checks["ollama"]["status"] in ("down", "timeout")

    any_degraded = any(
        v["status"] not in ("ok", "disabled", "disconnected")
        for v in checks.values()
    )

    if critical_down:
        http_code = 503
        overall = "critical"
    elif any_degraded:
        http_code = 207
        overall = "degraded"
    else:
        http_code = 200
        overall = "ok"

    return jsonify({
        "status":  overall,
        "checks":  checks,
        "summary": {
            "router9_ok": router9_ok,
            "ollama_ok":  ollama_ok,
            "vts_connected": checks["vts"]["status"] == "ok",
        },
    }), http_code
