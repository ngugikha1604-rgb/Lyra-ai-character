"""
app/services/__init__.py

Re-export các singleton để import gọn hơn:
    from app.services import audio_service, sse_service, stream_service, proactive_service
"""

from app.services.audio_service    import audio_service
from app.services.sse_service      import sse_service
from app.services.stream_service   import stream_service
from app.services.proactive_service import proactive_service

__all__ = [
    "audio_service",
    "sse_service",
    "stream_service",
    "proactive_service",
]
