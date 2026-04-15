# Conversation State Machine + Rhythm Detection for Lyra

import re
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
    r"\b(bye|goodbye|tạm biệt|chào nhé|thôi ngủ|đi ngủ|ngủ rồi|ok thôi|"
    r"thôi nha|thôi nhé|hẹn sau|later|gotta go|gtg|cya|see ya|"
    r"ok đó|oke đó|oke thôi|ok rồi|xong rồi|done|finished)\b",
    re.IGNORECASE,
)

_GOODBYE_PATTERNS = re.compile(
    r"\b(bye+|goodnight|good night|ngủ ngon|chúc ngủ ngon|tạm biệt nhé|"
    r"hẹn gặp lại|see you|ciao)\b",
    re.IGNORECASE,
)

# Topic-shift: semantic distance is expensive, so we use a cheap heuristic —
# a sudden short message after a long exchange, or explicit transition words.
_SHIFT_PATTERNS = re.compile(
    r"\b(anyway|btw|by the way|oh wait|à mà|ừ mà|thôi chuyển sang|"
    r"nói chuyện khác|đổi chủ đề|nhân tiện)\b",
    re.IGNORECASE,
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
        self._user_lengths.append(len(text))

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

        if avg <= 15:
            return "User writes very short messages. Match their brevity — 1 sentence max."
        if avg <= 40:
            return "User writes short-to-medium messages. Keep replies to 1-2 sentences."
        if avg <= 100:
            return "User writes medium-length messages. 2 sentences is fine."
        return "User writes longer messages. You can be slightly more expressive, but still concise."

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
