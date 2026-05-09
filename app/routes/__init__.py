"""
routes/__init__.py — Re-export tất cả blueprints để app factory import gọn.
"""

from app.routes.chat          import bp as chat_bp
from app.routes.tts           import bp as tts_bp
from app.routes.stream        import bp as stream_bp
from app.routes.stream_events import bp as stream_events_bp
from app.routes.auth          import bp as auth_bp
from app.routes.admin         import bp as admin_bp

__all__ = [
    "chat_bp",
    "tts_bp",
    "stream_bp",
    "stream_events_bp",
    "auth_bp",
    "admin_bp",
]
