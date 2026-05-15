import json
import re
from prompts import (
    STREAM_ROLLING_SUMMARY_PROMPT, STREAM_EVENT_SYSTEM, 
    STREAM_GREETING_PROMPT, STREAM_FAREWELL_PROMPT, PROACTIVE_STREAM_PROMPT
)
from config import STREAM_TITLE, STREAM_GAME, STREAM_GOALS, STREAM_NOTES

class StreamHandlerMixin:
    """
    Mixin for Lyra's stream-specific logic.
    Handles stream preparation, summaries, and event reactions.
    """

    def prepare_for_stream(self):
        """Prepares Lyra's state for a livestream warm-up."""
        print("[Core] Preparing state for livestream warm-up...")
        self.emotion.attention = 10
        self.memory.clear_session_memory()
        self.is_streaming = True
        self.stream_turn_counter = 0

    def save_memory(self):
        """Saves memory state to database."""
        self.memory._is_dirty = True
        self.memory.save()

    def update_stream_summary(self):
        """Summarizes stream events based on recent L2 session items."""
        try:
            session_items = self.memory._session_items
            if not session_items:
                return

            events_str = "\n".join([f"- {i['value']}" for i in session_items])
            messages = [
                {"role": "system", "content": STREAM_EVENT_SYSTEM},
                {"role": "user", "content": STREAM_ROLLING_SUMMARY_PROMPT.format(events=events_str)},
            ]

            # Dynamic max_tokens based on attention (50-150)
            _base_tokens = 50 + int(self.emotion.attention * 10)
            summary = self._call_light_model(messages, temperature=0.7, max_tokens=_base_tokens)

            if summary:
                self.memory.update_rolling_stream_summary(summary)
        except Exception as e:
            print(f"[Core] update_stream_summary error: {e}")

    def generate_stream_event_reply(self, event_type: str, context: dict = None, temperature: float = None) -> str:
        """
        Generates Lyra's reaction to a stream event.
        event_type: 'greeting' | 'farewell' | 'milestone' | 'regular_arrival' | 'silence_fill'
        """
        ctx = context or {}
        messages = []

        if event_type == "greeting":
            prompt_text = STREAM_GREETING_PROMPT.format(
                title=STREAM_TITLE or "stream hôm nay",
                game=STREAM_GAME or "chuyện phiếm",
                goals=", ".join(STREAM_GOALS) if STREAM_GOALS else "tâm sự với viewer",
                notes=STREAM_NOTES or "",
            )
            messages = [
                {"role": "system", "content": STREAM_EVENT_SYSTEM},
                {"role": "user", "content": prompt_text},
            ]
        elif event_type == "farewell":
            prompt_text = STREAM_FAREWELL_PROMPT.format(
                summary=ctx.get("summary", "stream vui vẻ"),
                top_viewers=ctx.get("top_viewers", "mọi người"),
                duration=ctx.get("duration", "một lúc"),
            )
            messages = [
                {"role": "system", "content": STREAM_EVENT_SYSTEM},
                {"role": "user", "content": prompt_text},
            ]
        elif event_type == "milestone":
            milestone_desc = ctx.get("description", "đạt milestone mới")
            messages = [
                {"role": "system", "content": STREAM_EVENT_SYSTEM},
                {"role": "user", "content": f"Stream event: {milestone_desc}. React ngắn gọn, tự nhiên."},
            ]
        elif event_type == "silence_fill":
            prompt_text = PROACTIVE_STREAM_PROMPT.format(
                current_activity="đang thả hồn ở đâu đó", # Thay đổi hoạt động mặc định
                game="nghĩ vẩn vơ", # Thay đổi game mặc định
            )
            messages = [
                {"role": "system", "content": STREAM_EVENT_SYSTEM},
                {"role": "user", "content": prompt_text},
            ]
        else:
            return ""

        try:
            # Dùng temperature truyền vào, nếu không dùng logic mặc định
            if temperature is not None:
                event_temp = temperature
            else:
                # Lower temperature for greetings (0.2) and others (0.3) for stability
                event_temp = 0.2 if event_type == "greeting" else 0.3
            
            reply = self._call_light_model(messages, temperature=event_temp, max_tokens=40)
            return self.clean_reply(reply or "")
        except Exception as e:
            print(f"[Stream Event] generate error: {e}")
            return ""

    def _generate_stream_plan(self):
        """(Disabled) Previously generated an agenda for the stream."""
        pass
