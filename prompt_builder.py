import random
from prompts import (
    CORE_SYSTEM_PROMPT, STREAM_VIEWER_PERSONALITY, RELATIONSHIP_HINTS,
    MOOD_HINTS, USER_MOOD_HINTS, INTENT_HINTS, VTUBER_BRAIN_INSTRUCTIONS,
    PERSONA_TIERS, IDEOLOGY_PROMPTS
)
from time_utils import get_time_context
from live_context import get_live_context_block

TOKEN_BUDGET_SAFE = 1200  # Max safe tokens for small models (~800-1200 effective window)


class PromptBuilderMixin:
    """
    Mixin for Lyra's prompt construction and response composition logic.
    Handles building system prompts and formatting user messages.
    """

    def _estimate_tokens(self, text: str) -> int:
        """Ước tính token count. Tiếng Việt char/3 là tỷ lệ an toàn cho subword tokenizers."""
        return len(text) // 3



    def build_prompt(
        self,
        intent,
        user_input,
        search_context="",
        source_type: str = "owner",
        viewer_data: dict = None,
        stream_context: str = "",
        loaded_skill_content: str = "",
        reward_hint="",
        active_inference_mode: str = None,
        perlocution_hint: str = "",
        self_disclosure_hint: str = "",
        precomputed_memory_context: str = None,
    ):
        """Constructs the system prompt based on state and memory.
        Uses a 4-tier greedy token budget system to keep prompts within
        TOKEN_BUDGET_SAFE tokens for small models, while preserving full
        assembly when context is small enough.
        """

        # ── Pre-compute all candidate sections ─────────────────────────────────
        # Always use CORE_SYSTEM_PROMPT to ensure full persona depth and learning
        base_personality = CORE_SYSTEM_PROMPT

        relationship_hint = (
            RELATIONSHIP_HINTS["very_close"] if self.emotion.affection > 70
            else RELATIONSHIP_HINTS["building"] if self.emotion.affection > 40
            else RELATIONSHIP_HINTS["new"]
        )
        source_context = self._build_source_context(source_type, viewer_data)
        _stream_ctx = stream_context or getattr(self, "stream_context", "") or ""

        is_public = source_type != "owner"
        memory_context = precomputed_memory_context if precomputed_memory_context is not None else self.memory.get_relevant_context(user_input, is_public=is_public)

        diary_hint = ""
        if not is_public:
            # Cache diary hint trong session — diary chỉ thay đổi khi kết thúc phiên hoặc write_diary_entry
            if getattr(self, '_diary_hint_cache', None) is None:
                recent_diaries = self.memory.get_diary_entries(limit=1)
                self._diary_hint_cache = f"\nCẢM XÚC GẦN ĐÂY CỦA LYRA:\nSuy nghĩ bí mật cuối cùng của em: '{recent_diaries[0]['content'][:150]}...'" if recent_diaries else ""
            diary_hint = self._diary_hint_cache

        full_memory_context = "\n\n".join(filter(None, [memory_context, search_context, diary_hint]))
        time_context = get_time_context(self.current_time, self.time_period)

        mood_hint = MOOD_HINTS["good"] if self.emotion.mood > 5 else MOOD_HINTS["bad"] if self.emotion.mood < -5 else ""

        user_hint = ""
        ai_mood = getattr(self, "_user_mood_today", None)
        if ai_mood:
            user_hint = f"Họ có vẻ đang {ai_mood} hôm nay."
        else:
            # Dùng giá trị đã detect ở đầu turn trong chat() để tránh redundant call
            user_mood = getattr(self, "_last_user_mood", "neutral")
            if user_mood in ("sad", "stressed", "anxious"):
                user_hint = USER_MOOD_HINTS["off"]
            elif user_mood == "excited":
                user_hint = USER_MOOD_HINTS["excited"]

        intent_hint = INTENT_HINTS.get(intent, "")

        # Anti-repetition logic
        last_reply = ""
        recent_patterns = set()
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                if not last_reply:
                    last_reply = msg.get("content", "")[:60]
                words = msg.get("content", "").strip().split()[:3]
                if words:
                    recent_patterns.add(" ".join(words).lower())
            if len(recent_patterns) >= 6:
                break

        anti_repeat_note = ""
        if last_reply:
            anti_repeat_note = f'Câu trả lời trước của em bắt đầu bằng: "{last_reply[:30]}...". Đừng bắt đầu câu này giống như vậy.'
        if recent_patterns:
            anti_repeat_note += f"\nTránh bắt đầu bằng các cụm từ vừa dùng gần đây: {list(recent_patterns)}."

        _session_ctx = self.memory.get_session_context() or ""

        # Conversation state & rhythm hints
        state_hint = self.conv_state.get_state_hint()
        rhythm_hint = self.conv_state.get_rhythm_hint()
        lsm_hint = self.conv_state.get_lsm_directive(dominance=self.emotion.dominance) if source_type == "owner" else ""
        conv_hints = "\n".join(filter(None, [state_hint, rhythm_hint, lsm_hint]))

        # Situation note
        situation_note = "TÌNH HUỐNG:\n"
        if self.is_streaming:
            situation_note += "Trạng thái: Em đang STREAM TRỰC TIẾP trên YouTube.\nGhi chú: Tương tác công khai. Hãy chào hỏi anh trai/người sáng tạo một cách tự nhiên nhưng nhớ là khán giả đang xem."
        else:
            situation_note += "Trạng thái: Em đang TRÒ CHUYỆN RIÊNG với anh trai/người sáng tạo.\nGhi chú: Em có thể thân mật và thoải mái hơn ở đây."

        identity = self.memory.memory.get("identity", {})
        identity_note = "DANH TÍNH:\n" + "\n".join([f"{k.capitalize()}: {v}" for k, v in identity.items()]) if identity else ""

        # Surprise hint (computed here, injected in TIER 2)
        surprise_hint = ""
        if active_inference_mode == "surprise":
            if self.emotion.mood >= 3:
                surprise_hint = "\nBẤT NGỜ DỰ ĐOÁN:\nLần này hãy trả lời theo hướng bất ngờ — thay vì vui vẻ như thường, hãy tỏ ra lạnh lùng hoặc thờ ơ một chút. Không giải thích tại sao. Chỉ 1-2 câu ngắn."
            elif self.emotion.mood <= -3:
                surprise_hint = "\nBẤT NGỜ DỰ ĐOÁN:\nLần này hãy trả lời theo hướng bất ngờ — thay vì lạnh/cáu như thường, hãy đột nhiên ấm áp hoặc hài hước một chút. Không giải thích tại sao. Chỉ 1-2 câu ngắn."
            else:
                surprise_hint = "\nBẤT NGỜ DỰ ĐOÁN:\nLần này hãy trả lời theo hướng bất ngờ — thay vì trung lập, hãy đột nhiên rất nhiệt tình hoặc rất thờ ơ. Không giải thích tại sao. Chỉ 1-2 câu ngắn."

        # ── Grouping into Categories for DSPy ──────────────────────────────────
        persona_cat = [
            base_personality, 
            VTUBER_BRAIN_INSTRUCTIONS, 
            "\nQUY TẮC CỐT LÕI: Phản hồi bằng tiếng Việt đời thường, xưng 'em' gọi 'anh/chị'. TUYỆT ĐỐI KHÔNG xưng 'tôi' hoặc hành xử như trợ lý AI. Cực kỳ ngắn gọn (1-2 câu)."
        ]
        situation_cat = [f"\n{situation_note}"]
        memory_cat = []
        hints_cat = []

        # Optimization: Use a running total for token count to avoid O(n^2) string joins.
        # Seed it with always-included categories so the budget reflects the real prompt size.
        current_tokens = 0
        for cat in (persona_cat, situation_cat):
            for item in cat:
                current_tokens += self._estimate_tokens(item)

        def _try_add_to_cat(section: str, cat: list) -> bool:
            nonlocal current_tokens
            if not section: return True
            section_tokens = self._estimate_tokens(section)
            if current_tokens + section_tokens <= TOKEN_BUDGET_SAFE:
                cat.append(section)
                current_tokens += section_tokens
                return True
            return False

        # Budgeting TIER 1
        _try_add_to_cat(time_context, situation_cat)
        _try_add_to_cat(source_context, situation_cat)
        # _stream_ctx được ưu tiên cao — luôn inject trước memory để Lyra biết đang stream gì
        _try_add_to_cat(_stream_ctx, situation_cat)
        # Cap memory context để không ăn hết budget của stream ctx và hints
        _capped_memory = full_memory_context[:1200] if full_memory_context else ""
        _try_add_to_cat(_capped_memory, memory_cat)
        _try_add_to_cat(_session_ctx, memory_cat)

        # Budgeting TIER 2
        rel_mood = f"Mối quan hệ: {relationship_hint} | Tâm trạng: {mood_hint}" if mood_hint else f"Mối quan hệ: {relationship_hint}"
        _try_add_to_cat(rel_mood, situation_cat)
        _try_add_to_cat(f"Người dùng: {user_hint}" if user_hint else "", situation_cat)
        _try_add_to_cat(f"Ý định: {intent_hint}" if intent_hint else "", situation_cat)
        _try_add_to_cat(conv_hints, hints_cat)
        _try_add_to_cat(anti_repeat_note, hints_cat)
        if perlocution_hint: _try_add_to_cat(perlocution_hint, hints_cat)
        if self_disclosure_hint: _try_add_to_cat(self_disclosure_hint, hints_cat)
        if reward_hint: _try_add_to_cat(reward_hint, hints_cat)
        if surprise_hint: _try_add_to_cat(surprise_hint, situation_cat)

        # Budgeting TIER 3
        _try_add_to_cat(get_live_context_block(), memory_cat)
        if identity_note: _try_add_to_cat(f"\nDANH TÍNH:\n{identity_note}", memory_cat)
        if source_type == "owner":
            _try_add_to_cat("KỸ NĂNG: " + (self.skills_index or "Không có"), hints_cat)
        if loaded_skill_content:
            _try_add_to_cat("\nNỘI DUNG KỸ NĂNG ĐÃ TẢI:\n" + loaded_skill_content, hints_cat)

        return {
            "persona": "\n".join(filter(None, persona_cat)),
            "situation": "\n".join(filter(None, situation_cat)),
            "memory": "\n".join(filter(None, memory_cat)),
            "behavior_hints": "\n".join(filter(None, hints_cat))
        }

    def _build_source_context(self, source_type: str, viewer_data: dict) -> str:
        """Helper to create context based on the message source (Owner vs Viewer)"""
        if source_type == "owner":
            return "Trạng thái: Em đang trò chuyện riêng với anh trai."

        data = viewer_data or {}
        name = data.get("viewer_name", "Một người xem")
        streams = data.get("total_streams", 1)
        gender = data.get("gender", "male")
        pronoun = "anh" if gender == "male" else "chị"

        if source_type == "regular_viewer":
            return f"Trạng thái: Em đang stream cho {name}, một người xem quen (đã xem {streams} buổi). Hãy xưng hô là '{pronoun}'."
        if source_type == "donor":
            amount = data.get("amount", "")
            amount_str = f" ({amount})" if amount else ""
            return f"Trạng thái: {name} vừa gửi Super Chat{amount_str}! Hãy phản hồi ấm áp, gọi tên họ và cảm ơn một cách tự nhiên. Xưng hô là '{pronoun}'."
        
        return f"Trạng thái: {name} là người xem mới. Hãy chào đón nhưng vẫn giữ đúng cá tính của mình. Xưng hô là '{pronoun}'."

    def compose_user_message(self, user_input, intent, reward_active: bool = False, ideology_idx: int = -1):
        """Composes the final user message with state tags."""
        if ideology_idx >= 0 and ideology_idx < len(IDEOLOGY_PROMPTS):
            ideology_q = IDEOLOGY_PROMPTS[ideology_idx]
            return f"{user_input}\n\n[QUY TẮC TÒ MÒ: GHI ĐÈ PHẢN HỒI]\nTạm thời lờ đi tin nhắn của anh ấy. Thay vào đó, hãy hỏi anh ấy câu hỏi sâu sắc/triết học này: \"{ideology_q}\"\nHãy giữ đúng cá tính! Chỉ hỏi một câu duy nhất. Không giải thích tại sao."

        parts = ["<ctx>"]
        parts.append(f"<t>{self.current_time.strftime('%H:%M %Z')}</t>")
        parts.append(f"<state>{self.emotion.describe_internal_state()}</state>")
        parts.append(f"<user>{self.infer_user_signal(user_input)}</user>")

        if intent == "introduction":
            parts.append("<note>Vừa cho biết tên. Dùng nó tự nhiên.</note>")

        if self.time_gap_hours and self.time_gap_hours >= 2:
            parts.append(f"<gap>{self.time_gap_hours:.1f}h qua. Hãy phản ứng tự nhiên.</gap>")

        parts.append("<rules>")
        if self.turn_counter > 1 and (self.time_gap_hours is None or self.time_gap_hours < 2):
            parts.append("KHÔNG chào hỏi sáo rỗng (Hi/Hello). Đi thẳng vào nội dung.")
        parts.append("CỰC NGẮN: 1-2 câu. KHÔNG xưng 'tôi/mình'. KHÔNG dùng ngôn ngữ máy móc.")
        parts.append("</rules>")

        if random.random() < 0.15:
            targets = self.memory.memory.get("facts", {}).get("goals", []) + self.memory.memory.get("facts", {}).get("topics", [])
            if targets:
                parts.append(f"<curiosity>Hỏi anh về '{random.choice(targets)}'.</curiosity>")

        parts.append("<persona>")
        aff = self.emotion.affection
        if aff < 20: parts.append(PERSONA_TIERS["distant"])
        elif aff < 45: parts.append(PERSONA_TIERS["acquaintance"])
        elif aff < 70: parts.append(PERSONA_TIERS["normal"])
        elif aff < 90: parts.append(PERSONA_TIERS["trusted"])
        else: parts.append(PERSONA_TIERS["clingy"])
        parts.append("</persona>")

        inside_jokes = self.memory.memory.get("facts", {}).get("inside_jokes", [])
        if inside_jokes:
            parts.append(f"<lore>Jokes: {', '.join(inside_jokes)}.</lore>")

        if intent == "choice":
            if random.random() < 0.10:
                parts.append("<decision>BƯỚNG: Từ chối cả hai. Đề xuất cái khác.</decision>")
            else:
                parts.append(f"<decision>{self.emotion.evaluate_decision_bias(self.time_period)}</decision>")

        parts.append("</ctx>")
        return f"{user_input}\n\n" + "\n".join(parts)

