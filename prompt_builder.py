import os
import re
import json
import time
import random
from datetime import datetime
from prompts import (
    NATURAL_BASE_PERSONALITY, STREAM_VIEWER_PERSONALITY, RELATIONSHIP_HINTS,
    MOOD_HINTS, USER_MOOD_HINTS, INTENT_HINTS, VTUBER_BRAIN_INSTRUCTIONS,
    PERSONA_TIERS, IDEOLOGY_PROMPTS, DIARY_GENERATION_PROMPT, THOUGHT_CHAIN_SYSTEM
)
from time_utils import get_time_context, get_weekend_context
from live_context import get_live_context_block

class PromptBuilderMixin:
    """
    Mixin for Lyra's prompt construction and response composition logic.
    Handles building system prompts and formatting user messages.
    """

    def _load_skill_index(self) -> str:
        index_path = os.path.join(self.skills_dir, "_index.md")
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _load_skill_content(self, skill_name: str) -> str:
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "", skill_name)
        skill_path = os.path.join(self.skills_dir, f"{safe_name}.md")
        if os.path.exists(skill_path):
            with open(skill_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _log_skill_usage(self, skill_name: str):
        """Updates skill usage statistics in JSON."""
        stats_path = os.path.join(self.skills_dir, "skill_stats.json")
        stats = {}
        if os.path.exists(stats_path):
            try:
                with open(stats_path, "r", encoding="utf-8") as f:
                    stats = json.load(f)
            except Exception:
                pass

        entry = stats.get(skill_name, {"call_count": 0, "first_used": time.time(), "description": "Kỹ năng tự học"})
        entry["call_count"] += 1
        entry["last_used"] = time.time()
        stats[skill_name] = entry

        with open(stats_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)

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
        """Constructs the system prompt based on state and memory."""
        # TIER 0: STATIC & FRAMEWORK
        base_personality = NATURAL_BASE_PERSONALITY if source_type == "owner" else STREAM_VIEWER_PERSONALITY

        # TIER 1: SESSION & RELATIONSHIP
        relationship_hint = (
            RELATIONSHIP_HINTS["very_close"] if self.emotion.affection > 70
            else RELATIONSHIP_HINTS["building"] if self.emotion.affection > 40
            else RELATIONSHIP_HINTS["new"]
        )
        source_context = self._build_source_context(source_type, viewer_data)
        _stream_ctx = stream_context or getattr(self, "stream_context", "") or ""

        # TIER 2: DYNAMIC CONTEXT
        is_public = source_type != "owner"
        memory_context = precomputed_memory_context if precomputed_memory_context is not None else self.memory.get_relevant_context(user_input, is_public=is_public)

        diary_hint = ""
        if not is_public:
            recent_diaries = self.memory.get_diary_entries(limit=1)
            if recent_diaries:
                diary_hint = f"\n[LYRA'S RECENT FEELINGS]\nYour last secret thought: '{recent_diaries[0]['content'][:150]}...'"

        full_memory_context = "\n\n".join(filter(None, [memory_context, search_context, diary_hint]))
        time_context = get_time_context(self.current_time, self.time_period)

        mood_hint = MOOD_HINTS["good"] if self.emotion.mood > 5 else MOOD_HINTS["bad"] if self.emotion.mood < -5 else ""

        user_hint = ""
        ai_mood = getattr(self, "_user_mood_today", None)
        if ai_mood:
            user_hint = f"They seem {ai_mood} today."
        else:
            user_mood = self.detect_user_mood(user_input)
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
            anti_repeat_note = f'- Your last reply started with: "{last_reply[:30]}...". Do NOT start this reply similarly.'
        if recent_patterns:
            anti_repeat_note += f"\n- Avoid starting with any of these patterns used recently: {list(recent_patterns)}."

        _session_ctx = self.memory.get_session_context() or ""

        # Conversation state & rhythm hints
        state_hint = self.conv_state.get_state_hint()
        rhythm_hint = self.conv_state.get_rhythm_hint()
        lsm_hint = self.conv_state.get_lsm_directive(dominance=self.emotion.dominance) if source_type == "owner" else ""
        conv_hints = "\n".join(filter(None, [state_hint, rhythm_hint, lsm_hint]))

        # Situation note
        situation_note = "[SITUATION]\n"
        if self.is_streaming:
            situation_note += "Status: You are currently STREAMING LIVE on YouTube.\nNote: Interaction is public. Acknowledge your creator/brother naturally but remember the audience is watching."
        else:
            situation_note += "Status: You are in a PRIVATE CONVERSATION with your creator/brother.\nNote: You can be more intimate and relaxed here."

        identity = self.memory.memory.get("identity", {})
        identity_note = "[IDENTITY]\n" + "\n".join([f"- {k.capitalize()}: {v}" for k, v in identity.items()]) if identity else ""

        # ASSEMBLY
        parts = [
            get_live_context_block(),
            base_personality, identity_note, VTUBER_BRAIN_INSTRUCTIONS, "\n" + situation_note,
            "\n[AVAILABLE SKILLS]", self._skills_index, time_context, "\n[SESSION INFO]",
            source_context, _stream_ctx, relationship_hint, mood_hint,
            user_hint, intent_hint, "\n[PERSONALITY GUIDELINES]",
            "- TRẢ LỜI BẰNG TIẾNG VIỆT. Không trả lời bằng tiếng Anh.",
            "- Let warmth, teasing, distance, or softness emerge naturally.",
            "- Be concise (1-2 sentences).", anti_repeat_note, conv_hints, full_memory_context, _session_ctx
        ]

        if perlocution_hint: parts.append(perlocution_hint)
        if self_disclosure_hint: parts.append(self_disclosure_hint)
        if reward_hint: parts.append(reward_hint)

        if active_inference_mode == "surprise":
            if self.emotion.mood >= 3:
                surprise_hint = "\n[PREDICTIVE SURPRISE]: Lần này hãy trả lời theo hướng bất ngờ — thay vì vui vẻ/playful như thường, hãy tỏ ra lạnh lùng hoặc thờ ơ một chút. Không giải thích tại sao. Chỉ 1-2 câu ngắn."
            elif self.emotion.mood <= -3:
                surprise_hint = "\n[PREDICTIVE SURPRISE]: Lần này hãy trả lời theo hướng bất ngờ — thay vì lạnh/cáu như thường, hãy đột nhiên ấm áp hoặc hài hước một chút. Không giải thích tại sao. Chỉ 1-2 câu ngắn."
            else:
                surprise_hint = "\n[PREDICTIVE SURPRISE]: Lần này hãy trả lời theo hướng bất ngờ — thay vì neutral, hãy đột nhiên rất nhiệt tình hoặc rất thờ ơ. Không giải thích tại sao. Chỉ 1-2 câu ngắn."
            parts.append(surprise_hint)

        if loaded_skill_content:
            parts.append("\n[LOADED SKILL CONTENT]\n" + loaded_skill_content)

        parts.append(f"\nCurrent status:\n- Intent: {intent}")
        return "\n".join(filter(None, parts))

    def _build_source_context(self, source_type: str, viewer_data: dict) -> str:
        """Helper to create context based on the message source (Owner vs Viewer)"""
        if source_type == "owner":
            return "Status: You are talking privately with your creator/brother."

        data = viewer_data or {}
        name = data.get("viewer_name", "A viewer")
        streams = data.get("total_streams", 1)
        gender = data.get("gender", "male")
        pronoun = "anh" if gender == "male" else "chị"

        if source_type == "regular_viewer":
            return f"Status: You are streaming to {name}, a regular viewer (seen {streams} sessions). Address them as '{pronoun}' in Vietnamese."
        if source_type == "donor":
            amount = data.get("amount", "")
            amount_str = f" ({amount})" if amount else ""
            return f"Status: {name} just sent a Super Chat{amount_str}! React warmly, say their name, and acknowledge the donation naturally. Address them as '{pronoun}' in Vietnamese."
        
        return f"Status: {name} is a new viewer. Be welcoming but keep your core personality. Address them as '{pronoun}' in Vietnamese."

    def compose_user_message(self, user_input, intent, reward_active: bool = False, ideology_idx: int = -1):
        """Composes the final user message with state tags."""
        if ideology_idx >= 0 and ideology_idx < len(IDEOLOGY_PROMPTS):
            ideology_q = IDEOLOGY_PROMPTS[ideology_idx]
            return f"{user_input}\n\n[CURIOSITY RULE: OVERRIDE REPLY]\nIgnore his message for a moment. Instead, ask him this deep/existential question: \"{ideology_q}\"\nStay in character! Only ask one question. Don't explain why."

        parts = ["<context>"]
        parts.append(f"<time>{self.current_time.strftime('%A %H:%M %Z')}</time>")
        parts.append(f"<time_period>{self.time_period}</time_period>")
        parts.append(f"<weekday_context>{get_weekend_context(self.current_time)}</weekday_context>")
        parts.append(f"<lyra_internal_state>{self.emotion.describe_internal_state()}</lyra_internal_state>")
        parts.append(f"<user_signal>{self.infer_user_signal(user_input)}</user_signal>")

        if intent == "introduction":
            parts.append("<conversation_note>The user may have just given their name. Use it naturally if it fits.</conversation_note>")

        if self.time_gap_hours and self.time_gap_hours >= 2:
            parts.append(f"<recent_gap>{self.time_gap_hours:.1f} hours since the last exchange. Let it influence the mood only if it feels natural.</recent_gap>")

        parts.append("<critical_rules>")
        if self.turn_counter > 1 and (self.time_gap_hours is None or self.time_gap_hours < 2):
            parts.append("- DO NOT use ANY greeting (no 'Hey', 'Hi', 'Hello'). Start your message instantly with your thought.")
        parts.append("- BE CONCISE: Stop immediately after 1-2 short sentences. No rambling, no over-explaining, no filler.")
        parts.append("- DO NOT offer to 'tackle it together', 'break it down', or act like a tutor/therapist. You are a lazy 16yo sibling, not an AI assistant.")
        parts.append("</critical_rules>")

        if random.random() < 0.15:
            targets = self.memory.memory.get("facts", {}).get("goals", []) + self.memory.memory.get("facts", {}).get("topics", [])
            if targets:
                parts.append(f"<curiosity_rule>CRITICAL: DO NOT just answer! Randomly ask the user for an update about '{random.choice(targets)}'. Keep it natural.</curiosity_rule>")

        parts.append("<persona_rule>")
        aff = self.emotion.affection
        if aff < 20: parts.append(PERSONA_TIERS["distant"])
        elif aff < 45: parts.append(PERSONA_TIERS["acquaintance"])
        elif aff < 70: parts.append(PERSONA_TIERS["normal"])
        elif aff < 90: parts.append(PERSONA_TIERS["trusted"])
        else: parts.append(PERSONA_TIERS["clingy"])
        parts.append("</persona_rule>")

        inside_jokes = self.memory.memory.get("facts", {}).get("inside_jokes", [])
        if inside_jokes:
            parts.append(f"<lore>Inside Jokes: {', '.join(inside_jokes)}. Reference them organically ONLY if it fits the conversation.</lore>")

        if intent == "choice":
            if random.random() < 0.10:
                parts.append("<decision_rule>STUBBORN MODE: Reject both choices. Propose something completely different or tell them to stop overthinking.</decision_rule>")
            else:
                parts.append(f"<decision_rule>PROACTIVE CHOICE: {self.emotion.evaluate_decision_bias(self.time_period)}</decision_rule>")

        parts.append("</context>")
        return f"{user_input}\n\n" + "\n".join(parts)

    def write_diary_entry(self):
        """Generates and saves a secret diary entry after a session."""
        try:
            print("[Core] Writing secret diary...")
            session_ctx = self.memory.get_session_context()
            if not session_ctx:
                summaries = self.memory.get_diary_entries(limit=3)
                session_ctx = "\n".join([d["content"] for d in summaries])

            prompt = DIARY_GENERATION_PROMPT.format(
                session_summary=session_ctx[:800],
                emotion_state=self.emotion.describe_internal_state(),
                affection_level=f"{int(self.emotion.affection)}/100",
                turns=self.turn_counter,
            )

            entry_content = self._call_light_model([
                {"role": "system", "content": "Bạn là Lyra đang viết nhật ký. Trả về plain text."},
                {"role": "user", "content": prompt},
            ])

            if entry_content and len(entry_content.strip()) > 10:
                self.memory.add_diary_entry(
                    content=entry_content.strip(),
                    mood=self.emotion.mood,
                    affection=self.emotion.affection,
                )
                print("=" * 60 + "\n✅ SECRET DIARY - ĐÃ GHI XONG!\n" + "─" * 60 + f"\n{entry_content.strip()}\n" + "=" * 60)
                return True
            return False
        except Exception as e:
            print(f"[Core] write_diary_entry error: {e}")
            return False
