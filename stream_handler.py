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

    def generate_stream_event_reply(self, event_type: str, context: dict = None) -> str:
        """
        Generates Lyra's reaction to a stream event.
        event_type: 'greeting' | 'farewell' | 'milestone' | 'regular_arrival' | 'silence_fill'
        """
        ctx = context or {}
        messages = []

        if event_type == "greeting":
            goals_str = ", ".join(STREAM_GOALS) if STREAM_GOALS else "chưa có mục tiêu cụ thể"
            prompt_text = STREAM_GREETING_PROMPT.format(
                title=STREAM_TITLE or "stream hôm nay",
                game=STREAM_GAME or "chưa rõ",
                goals=goals_str,
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
                current_activity=ctx.get("current_activity", "đang chơi game"),
                game=STREAM_GAME or "game",
            )
            messages = [
                {"role": "system", "content": STREAM_EVENT_SYSTEM},
                {"role": "user", "content": prompt_text},
            ]
        else:
            return ""

        try:
            reply = self._call_light_model(messages, temperature=0.9, max_tokens=60)
            return self.clean_reply(reply or "")
        except Exception as e:
            print(f"[Stream Event] generate error: {e}")
            return ""

    def _generate_stream_plan(self):
        """Generates a 3-5 item agenda for the current stream session."""
        try:
            from live_context import update_plan
            
            print("[Core] Generating dynamic stream plan...")
            goals_str = ", ".join(STREAM_GOALS) if STREAM_GOALS else "chưa có"
            
            prompt = (
                f"Bạn là Lyra. Hãy tạo một bản kế hoạch (Agenda) cho buổi stream hôm nay.\n"
                f"Tiêu đề: {STREAM_TITLE or 'Không có'}\n"
                f"Game: {STREAM_GAME or 'Không có'}\n"
                f"Mục tiêu ban đầu: {goals_str}\n"
                f"Ghi chú: {STREAM_NOTES or 'Không có'}\n\n"
                f"Hãy tạo 3-5 mục tiêu nhỏ, cụ thể và 'nhập vai' (ví dụ: 'Trêu chủ nhân khi thua game', 'Hỏi thăm 3 bạn viewer mới').\n"
                f"Trả về JSON: {{\"plan\": [\"mục tiêu 1\", \"mục tiêu 2\"]}}"
            )
            
            raw = self._call_light_model([
                {"role": "system", "content": "Bạn là planner cho Lyra. Chỉ trả về JSON."},
                {"role": "user", "content": prompt}
            ], temperature=0.7, max_tokens=250)
            
            if raw:
                match = re.search(r'\{.*\}', raw, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    plan_texts = data.get("plan", [])
                    if plan_texts:
                        structured_plan = [{"goal": text, "status": "pending"} for text in plan_texts]
                        update_plan(structured_plan)
                        print(f"[Plan] Stream plan generated: {len(structured_plan)} items.")
        except Exception as e:
            print(f"[Core] generate_stream_plan error: {e}")
