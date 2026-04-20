# Conversation State Machine + Rhythm Detection for Lyra

import re
import random
from collections import deque


# ─── States ───────────────────────────────────────────────────────────────────
STATE_GREETING   = "greeting"
STATE_BUILDING   = "building"
STATE_DEEPENING  = "deepening"
STATE_SHIFTING   = "shifting"
STATE_CLOSING    = "closing"
STATE_GOODBYE    = "goodbye"


# ─── Closing / Goodbye signals ────────────────────────────────────────────────
_CLOSING_PATTERNS = re.compile(
    r"\b(bye|goodbye|chào nhé|thôi ngủ|đi ngủ|ngủ rồi|ok thôi|"
    r"thôi nha|thôi nhé|hẹn sau|later|gotta go|gtg|cya|see ya|"
    r"ok đó|oke đó|oke thôi|ok rồi|xong rồi|done|finished|"
    r"tắt máy|đi rồi|đi đây|thôi đi|ra ngoài rồi|bận rồi|"
    r"oke bye|ok bye|thôi nhé mình đi|mình đi đây)\b",
    re.IGNORECASE,
)

_GOODBYE_PATTERNS = re.compile(
    r"\b(bye+|goodnight|good night|ngủ ngon|chúc ngủ ngon|"
    r"tạm biệt|tạm biệt nhé|hẹn gặp lại|see you|ciao|"
    r"bái bai|baibai|bai bai|chào tạm biệt|hẹn hôm sau)\b",
    re.IGNORECASE,
)

# Vocabulary / Complexity Patterns
_SLANG_PATTERNS = re.compile(
    r"\b(vl|cl|vãi|đỉnh|chúa hề|vibe|chill|mlem|gấu|crush|cringe|phốt|drama|quẩy|xịn|mướt)\b",
    re.IGNORECASE
)

_INTELLECTUAL_PATTERNS = re.compile(
    r"\b(thuật toán|tâm lý|vĩ mô|vi mô|logic|phân tích|triết học|hệ tư tưởng|entropy|nhận thức|đồng bộ|kiến trúc|vận hành|hệ thống|mô hình|trừu tượng)\b",
    re.IGNORECASE
)

_SHIFT_PATTERNS = re.compile(
    r"\b(mà nè|à mà|đổi chủ đề|chuyện khác|sang chuyện|nhân tiện|by the way|anyway)\b",
    re.IGNORECASE
)


class ConversationStateDetector:
    """
    Tracks the current state of the conversation and provides
    rhythm (message-length) statistics for prompt injection.
    """

    def __init__(self, window: int = 10):
        # Rolling window of recent user message lengths
        self._user_lengths: deque[int] = deque(maxlen=window)
        self._state: str = STATE_GREETING
        self._turn: int = 0
        
        # Style / Complexity scores
        self._slang_count: int = 0
        self._intellectual_count: int = 0
        
        # Reward Schedule (Variable Ratio Reinforcement)
        self._last_reward_turn: int = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    def update(self, user_input: str, messages: list) -> str:
        """
        Call once per turn with the raw user message and the current
        message history.  Returns the new state string.
        """
        self._turn += 1
        text = (user_input or "").strip()
        # Only track lengths of substantial messages to avoid the "brevity death spiral"
        if len(text) > 3:
            self._user_lengths.append(len(text))

        # Update scoring (simple additive with slow decay)
        if _SLANG_PATTERNS.search(text):
            self._slang_count = min(self._slang_count + 2, 10)
        else:
            self._slang_count = max(0, self._slang_count - 1)

        if _INTELLECTUAL_PATTERNS.search(text):
            self._intellectual_count = min(self._intellectual_count + 2, 10)
        else:
            self._intellectual_count = max(0, self._intellectual_count - 1)

        # ── Vocabulary Scoring 2.0 (spec refinement) ──
        words = [w for w in re.findall(r"\w+", text.lower()) if len(w) >= 2]
        if len(words) >= 5:
            unique_ratio = len(set(words)) / len(words)
            avg_word_len = sum(len(w) for w in words) / len(words)
            
            # If user uses many unique, long words -> increase intellectual score
            if unique_ratio > 0.8 and avg_word_len > 4.5:
                self._intellectual_count = min(self._intellectual_count + 1, 10)
            # If user uses short, repetitive words -> increase slang/casual score
            elif avg_word_len < 3.5:
                self._slang_count = min(self._slang_count + 1, 10)

        self._state = self._detect_state(text, messages)
        return self._state

    def get_rhythm_hint(self) -> str:
        """
        Returns a short instruction string for the system prompt based on
        the user's average message length over the rolling window.
        """
        if not self._user_lengths:
            return ""

        avg = sum(self._user_lengths) / len(self._user_lengths)
        
        # Complexity Hint (Mirroring)
        complexity_note = ""
        if self._intellectual_count >= 4:
            complexity_note = " User's vocabulary is intellectual/deep. Match their depth, be more philosophical."
        elif self._slang_count >= 4:
            complexity_note = " User is using teen slang/casual vibe. Match their zoomer energy, use casual Vietnamese."

        if avg <= 15:
            return f"User writes very short messages. Match their brevity — 1 sentence max.{complexity_note}"
        if avg <= 40:
            return f"User writes short-to-medium messages. Keep replies to 1-2 sentences.{complexity_note}"
        if avg <= 100:
            return f"User writes medium-length messages. 2 sentences is fine.{complexity_note}"
        return f"User writes longer messages. You can be slightly more expressive, but still concise.{complexity_note}"

    def get_state_hint(self) -> str:
        """
        Returns a short instruction string for the system prompt based on
        the current conversation state.
        """
        hints = {
            STATE_GREETING:  "This is the start of the conversation. A brief, natural acknowledgment is enough.",
            STATE_BUILDING:  "The conversation is warming up. Follow their lead, ask at most one follow-up if it feels natural.",
            STATE_DEEPENING: "The conversation has depth now. You can reference past context or go a bit further.",
            STATE_SHIFTING:  "The topic just changed. Adapt quickly, don't drag the old topic.",
            STATE_CLOSING:   "They seem to be wrapping up. Keep it short, don't open new threads.",
            STATE_GOODBYE:   "They are saying goodbye. Respond warmly but briefly. Do NOT ask questions.",
        }
        return hints.get(self._state, "")

    def get_temperature(self, base_mood: float, base_attention: float) -> float:
        """
        Dynamic temperature based on emotion state + conversation state.

        Ranges:
          - closing / goodbye  → lower (more predictable, safe)
          - deepening          → medium (balance creativity & consistency)
          - bored (low attn)   → slightly higher (seek variety)
          - angry (mood < -5)  → higher (allow rawness)
          - excited (mood > 5) → slightly higher (more expressive)
          - default            → 0.80
        """
        temp = 0.80

        # State-based adjustment
        if self._state in (STATE_CLOSING, STATE_GOODBYE):
            temp = 0.60
        elif self._state == STATE_DEEPENING:
            temp = 0.75
        elif self._state == STATE_SHIFTING:
            temp = 0.85

        # Emotion-based adjustment (layered on top)
        if base_attention <= 2:
            temp = min(temp + 0.10, 1.10)   # bored → more random
        if base_mood <= -5:
            temp = min(temp + 0.10, 1.10)   # angry → rawer
        if base_mood >= 6:
            temp = min(temp + 0.05, 1.00)   # excited → slightly more expressive

        return round(temp, 2)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _detect_state(self, text: str, messages: list) -> str:
        # Hard goodbye
        if _GOODBYE_PATTERNS.search(text):
            return STATE_GOODBYE

        # Closing signals
        if _CLOSING_PATTERNS.search(text):
            return STATE_CLOSING

        # Topic shift
        if _SHIFT_PATTERNS.search(text) and self._turn > 3:
            return STATE_SHIFTING

        # Very first turn or after a long gap (caller resets turn counter)
        if self._turn <= 1:
            return STATE_GREETING

        # Count assistant turns to gauge depth
        assistant_turns = sum(
            1 for m in messages
            if isinstance(m, dict) and m.get("role") == "assistant"
        )

        if assistant_turns <= 2:
            return STATE_BUILDING
        if assistant_turns >= 6:
            return STATE_DEEPENING

        # Default: keep previous state (stability)
        return self._state

    def should_trigger_reward(self, probability: float = 0.07) -> bool:
        """
        Implements Variable Ratio Reinforcement schedule.
        Returns True if a 'micro-reward' (compliment, debate, etc.) should be triggered.
        """
        # Cooldown of at least 3 turns to keep it surprising but not spammy
        if self._turn - self._last_reward_turn < 3:
            return False
            
        if random.random() < probability:
            self._last_reward_turn = self._turn
            return True
        return False
