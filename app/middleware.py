"""
app/middleware.py — Security middleware và decorators.

  after_request_handler  — security headers (X-Frame-Options, etc.)
  require_auth           — decorator kiểm tra X-Admin-Key header
  sanitize_input         — re-export từ helpers (convenience)
"""

from __future__ import annotations

import functools
import os

from flask import jsonify, request


# ------------------------------------------------------------------ #
# Admin auth decorator                                                 #
# ------------------------------------------------------------------ #

_ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY")
if not _ADMIN_API_KEY:
    raise RuntimeError(
        "[Security] ADMIN_API_KEY chưa được set trong .env!\n"
        "Tạo key ngẫu nhiên:\n"
        "  python -c \"import secrets; print(secrets.token_hex(32))\""
    )


def require_auth(f):
    """Decorator: yêu cầu X-Admin-Key header khớp với ADMIN_API_KEY trong .env."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-Admin-Key")
        if not key or key != _ADMIN_API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ------------------------------------------------------------------ #
# Security headers                                                     #
# ------------------------------------------------------------------ #

def apply_security_headers(response):
    """Gắn vào app.after_request trong create_app()."""
    response.headers["X-Frame-Options"]        = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"]       = "1; mode=block"
    return response
