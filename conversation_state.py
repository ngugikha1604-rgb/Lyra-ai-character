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
        
        # ── Variable Ratio Reinforcement (Skinner) ────────────────────────
        self._last_reward_turn: int = 0       # turn khi reward được DELIVER (set bởi confirm)
        self._last_reward_attempt_turn: int = 0  # turn khi reward được ATTEMPT (set bởi should_trigger)
        # Behavioral shaping: đếm hành vi tích cực liên tiếp của user
        # Tăng xác suất reward khi user engage sâu (tin nhắn dài, câu hỏi, liên tiếp)
        self._positive_behavior_streak: int = 0  # 0 → N, reset khi user passive

        # ── Active Inference (Phần 4) ──────────────────────────────────────
        # Ideology: cooldown + no-repeat tracking
        self._last_ideology_turn: int = 0
        self._used_ideology_indices: list = []   # indices đã dùng trong session

        # Predictive Surprise: cooldown
        self._last_surprise_turn: int = 0

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

        # ── Behavioral Shaping: track positive engagement streak ──────────
        # Positive behavior: tin nhắn dài (>40 chars), có câu hỏi, hoặc intellectual
        is_engaged = (
            len(text) > 40
            or "?" in text
            or _INTELLECTUAL_PATTERNS.search(text)
        )
        if is_engaged:
            self._positive_behavior_streak = min(self._positive_behavior_streak + 1, 8)
        else:
            # Decay chậm — không reset ngay khi 1 tin nhắn ngắn
            self._positive_behavior_streak = max(0, self._positive_behavior_streak - 1)

        return self._state

    def get_vibe_tier(self) -> str:
        """
        Cognitive Entrainment — Vibe Sync.
        Returns the current detected communication style of the user.
        'slang'        → teen/casual Vietnamese (vl, chúa hề, chill...)
        'intellectual' → formal/academic/deep (thuật toán, triết học...)
        'neutral'      → default mixed style
        """
        if self._intellectual_count >= 4:
            return "intellectual"
        if self._slang_count >= 4:
            return "slang"
        return "neutral"

    def get_rhythm_hint(self) -> str:
        """
        Returns a short instruction string for the system prompt based on
        the user's average message length over the rolling window.
        """
        if not self._user_lengths:
            return ""

        avg = sum(self._user_lengths) / len(self._user_lengths)
        tier = self.get_vibe_tier()

        # ── Vibe Sync: tier-specific mirroring instruction ──────────────────
        if tier == "slang":
            vibe_note = (
                " [VIBE SYNC — SLANG]: User đang dùng ngôn ngữ teen/casual (vl, đỉnh, chill...). "
                "Mirror their energy: dùng từ lóng tự nhiên, viết tắt, emoji nếu hợp. "
                "Đừng formal, đừng giải thích dài dòng."
            )
        elif tier == "intellectual":
            vibe_note = (
                " [VIBE SYNC — DEEP]: User đang dùng từ ngữ học thuật/sâu sắc. "
                "Match their depth: dùng từ chính xác, có thể đặt câu hỏi triết học, "
                "tránh dùng slang hoặc emoji."
            )
        else:
            vibe_note = ""

        # ── Pace Sync: length-based instruction ─────────────────────────────
        if avg <= 15:
            return f"User writes very short messages. Match their brevity — 1 sentence max.{vibe_note}"
        if avg <= 40:
            return f"User writes short-to-medium messages. Keep replies to 1-2 sentences.{vibe_note}"
        if avg <= 100:
            return f"User writes medium-length messages. 2 sentences is fine.{vibe_note}"
        return f"User writes longer messages. You can be slightly more expressive, but still concise.{vibe_note}"

    def get_pace_max_tokens(self, base_tokens: int) -> int:
        """
        Cognitive Entrainment — Pace Sync.
        Adjusts the base token limit (from EmotionEngine) based on the user's
        current message pace (avg length from rolling window).

        Design rule: attention (Lyra's fatigue) is a SOFT ceiling.
        - Scale DOWN freely to mirror user brevity (always safe).
        - Scale UP only when Lyra is energized (attention tracked via base_tokens >= 100),
          preventing a tired Lyra from being forced to write long replies.

        Logic:
          - avg <= 15  → base * 0.7   (user very brief → mirror)
          - avg <= 40  → base         (unchanged)
          - avg <= 100 → base * 1.2   (medium — only if base >= 70, i.e. not tired)
          - avg > 100  → base * 1.5   (long — only if base >= 100, i.e. energized)

        Always clamps to [30, 180].
        """
        if not self._user_lengths:
            return base_tokens

        avg = sum(self._user_lengths) / len(self._user_lengths)

        if avg <= 15:
            adjusted = int(base_tokens * 0.7)
        elif avg <= 40:
            adjusted = base_tokens
        elif avg <= 100:
            # Chỉ mở rộng nếu Lyra không mệt (base >= 70)
            adjusted = int(base_tokens * 1.2) if base_tokens >= 70 else base_tokens
        else:
            # Chỉ mở rộng tối đa nếu Lyra hào hứng (base >= 100)
            adjusted = int(base_tokens * 1.5) if base_tokens >= 100 else base_tokens

        return max(30, min(180, adjusted))

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

    def get_temperature(self, base_mood: float, base_attention: float, dominance: float = 0.5) -> float:
        """
        Dynamic temperature based on emotion state + conversation state + vibe tier + dominance.

        Ranges:
          - closing / goodbye  → lower (more predictable, safe)
          - deepening          → medium (balance creativity & consistency)
          - bored (low attn)   → slightly higher (seek variety)
          - angry (mood < -5)  → higher (allow rawness)
          - excited (mood > 5) → slightly higher (more expressive)
          - slang vibe         → +0.08 (casual, raw, less filtered)
          - intellectual vibe  → -0.08 (precise, consistent, thoughtful)
          - low dominance      → -0.05 (uncertain → more careful, less random)
          - high dominance     → +0.05 (confident → more expressive)
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

        # ── Vibe Sync: mirror user's communication energy ──────────────────
        tier = self.get_vibe_tier()
        if tier == "slang":
            temp = min(temp + 0.08, 1.10)   # casual/raw → less filtered
        elif tier == "intellectual":
            temp = max(temp - 0.08, 0.55)   # deep/precise → more consistent

        # ── VAD Dominance: confidence level affects output randomness ──────
        if dominance <= 0.3:
            temp = max(temp - 0.05, 0.55)   # uncertain → more careful
        elif dominance >= 0.75:
            temp = min(temp + 0.05, 1.10)   # confident → more expressive

        return round(temp, 2)

    def should_trigger_ideology(self, total_prompts: int, min_cooldown: int = 5) -> int:
        """
        Active Inference — Ideological Proactivity.
        Returns index của câu hỏi ideology cần dùng, hoặc -1 nếu không trigger.

        Rules:
        - Cooldown tối thiểu `min_cooldown` turns giữa 2 lần trigger.
        - Không lặp lại câu đã dùng trong session.
        - Khi đã dùng hết tất cả câu → reset để cycle lại.
        - Có random roll bên trong (15%) — caller không cần roll thêm.
        """
        if self._turn - self._last_ideology_turn < min_cooldown:
            return -1

        # Random roll bên trong — 15% chance
        if random.random() >= 0.15:
            return -1

        # Reset nếu đã dùng hết tất cả
        if len(self._used_ideology_indices) >= total_prompts:
            self._used_ideology_indices = []

        available = [i for i in range(total_prompts) if i not in self._used_ideology_indices]
        if not available:
            return -1

        idx = random.choice(available)
        self._used_ideology_indices.append(idx)
        self._last_ideology_turn = self._turn
        return idx

    def should_trigger_surprise(self, probability: float = 0.05, min_cooldown: int = 5) -> bool:
        """
        Active Inference — Predictive Surprise.
        Returns True nếu lượt này Lyra nên subvert expectations.

        Rules:
        - 5% chance mỗi turn.
        - Cooldown tối thiểu `min_cooldown` turns để không spam.
        """
        if self._turn - self._last_surprise_turn < min_cooldown:
            return False

        if random.random() < probability:
            self._last_surprise_turn = self._turn
            return True
        return False

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

    def should_trigger_reward(self, probability: float = 0.07) -> "str | None":
        """
        Variable Ratio Reinforcement (Skinner) — trả về reward type hoặc None.

        Behavioral shaping: xác suất tăng theo positive_behavior_streak.
        Mỗi streak level +1 thêm 1.5% vào base probability (cap ở streak=8 → +12%).

        Reward weights:
          deep_recall:    40% — nhắc kỷ niệm hiếm
          healthy_debate: 25% — phản biện nhẹ
          vulnerability:  15% — bộc lộ điểm yếu (chỉ khi deepening)
          curiosity_spike: 10% — hỏi ngược bất ngờ
          silent_approval: 10% — im lặng tán thưởng

        NOTE: Cooldown (_last_reward_turn) KHÔNG được set ở đây.
        Caller (core.py) phải gọi confirm_reward_delivered() sau khi
        reward thực sự được inject vào prompt. Tránh consume cooldown
        khi reward bị skip do điều kiện context không thỏa.

        Returns: reward type string hoặc None nếu không trigger.
        """
        if self._turn - self._last_reward_turn < 3:
            return None
        # Tránh re-roll liên tục khi reward bị skip — cooldown attempt 1 turn
        if self._turn - self._last_reward_attempt_turn < 1:
            return None

        # Behavioral shaping: tăng xác suất theo streak
        shaped_probability = probability + self._positive_behavior_streak * 0.015
        shaped_probability = min(shaped_probability, 0.22)  # cap ở 22%

        if random.random() >= shaped_probability:
            return None

        # Weighted selection — chỉ include candidates có weight > 0
        # vulnerability chỉ available khi state == "deepening"
        candidates = [
            ("deep_recall",     0.40),
            ("healthy_debate",  0.25),
            ("curiosity_spike", 0.10),
            ("silent_approval", 0.10),
        ]
        if self._state == "deepening":
            candidates.append(("vulnerability", 0.15))

        # Normalize weights để tổng luôn = 1.0 (tránh overflow → fallback bias)
        total = sum(w for _, w in candidates)
        # total luôn > 0 vì candidates không bao giờ rỗng

        roll = random.random() * total
        cumulative = 0.0
        chosen = candidates[-1][0]  # fallback = phần tử cuối (tránh hardcode)
        for reward_type, weight in candidates:
            cumulative += weight
            if roll <= cumulative:
                chosen = reward_type
                break

        # Set attempt turn — tránh re-roll cùng turn nếu caller gọi lại
        # Cooldown deliver (_last_reward_turn) chỉ set bởi confirm_reward_delivered()
        self._last_reward_attempt_turn = self._turn
        return chosen

    def confirm_reward_delivered(self):
        """
        Gọi từ core.py sau khi reward_hint đã được set thành công.
        Chỉ khi này mới consume cooldown — tránh lãng phí slot khi reward bị skip.
        """
        self._last_reward_turn = self._turn
