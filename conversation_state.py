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

        # ── LSM Tracker (Communication Accommodation Theory) ──────────────
        # Chiều expressiveness: track mức độ biểu cảm của user (emoji, !!!, cảm xúc mạnh)
        # Các chiều khác (slang/intellectual/brevity) đã được cover bởi _slang_count,
        # _intellectual_count, và _user_lengths — không duplicate.
        self._expressiveness_score: float = 0.0  # 0.0 → 10.0, decay mỗi turn

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

        # ── LSM Expressiveness tracking ───────────────────────────────────
        # Signals: emoji, nhiều dấu !, ALL CAPS, từ cảm xúc mạnh
        _emoji_count = len(re.findall(
            r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
            r"\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF"
            r"\U0001FA70-\U0001FAFF]", text
        ))
        _exclaim_count = text.count("!")
        _has_caps = bool(re.search(r"[A-Z]{3,}", text))  # 3+ chữ hoa liên tiếp
        _expressive_words = bool(re.search(
            r"\b(omg|wow|wtf|lol|haha|hihi|hehe|ơi trời|trời ơi|ôi|ối|ồ)\b",
            text.lower()
        ))

        expressiveness_delta = (
            min(_emoji_count, 3) * 1.5      # tối đa +4.5 từ emoji
            + min(_exclaim_count, 3) * 0.8  # tối đa +2.4 từ !
            + (1.0 if _has_caps else 0.0)
            + (1.0 if _expressive_words else 0.0)
            - 0.5                           # natural drain mỗi turn
        )
        self._expressiveness_score = max(0.0, min(10.0,
            self._expressiveness_score + expressiveness_delta
        ))

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
                " ĐỒNG BỘ PHONG CÁCH — TỰ NHIÊN: Người dùng đang dùng ngôn ngữ đời thường/bình dân (vl, đỉnh, chill...). "
                "Hãy bắt chước năng lượng của họ: dùng từ lóng tự nhiên, viết tắt, hoặc biểu tượng cảm xúc nếu hợp. "
                "Đừng trang trọng, đừng giải thích dài dòng."
            )
        elif tier == "intellectual":
            vibe_note = (
                " ĐỒNG BỘ PHONG CÁCH — SÂU SẮC: Người dùng đang dùng từ ngữ có chiều sâu hoặc học thuật. "
                "Hãy bắt khớp với độ sâu của họ: dùng từ chính xác, có thể hỏi về triết học hoặc quan điểm, "
                "tránh dùng từ lóng quá đà."
            )
        else:
            vibe_note = ""

        # ── Pace Sync: length-based instruction ─────────────────────────────
        if avg <= 15:
            return f"Người dùng viết rất ngắn. Hãy đáp lại cực kỳ ngắn gọn — tối đa 1 câu.{vibe_note}"
        if avg <= 40:
            return f"Người dùng viết ngắn. Giữ câu trả lời trong khoảng 1-2 câu ngắn.{vibe_note}"
        if avg <= 100:
            return f"Người dùng viết ở mức trung bình. Có thể trả lời 1-2 câu đầy đủ hơn.{vibe_note}"
        return f"Người dùng viết khá dài. Em có thể biểu đạt nhiều hơn một chút, nhưng vẫn phải súc tích.{vibe_note}"

    def get_lsm_directive(self, dominance: float = 0.5) -> str:
        """
        LSM Tracker — Communication Accommodation Theory (Giles/Pennebaker).

        Tổng hợp tất cả style dimensions thành 1 directive string để inject
        vào system prompt. Quyết định Lyra nên CONVERGE (mirror user) hay
        DIVERGE (giữ nét riêng) dựa trên:
          - Vibe tier (slang/intellectual) — từ _slang_count/_intellectual_count
          - Expressiveness — từ _expressiveness_score
          - Conversation depth — từ _state
          - Lyra's confidence — từ dominance (VAD)

        Divergence trigger khi:
          - User đang ở extreme style (score >= 8) VÀ conversation đã deepening
          - Lyra đủ tự tin (dominance >= 0.65) để giữ nét riêng

        Returns: directive string hoặc "" nếu không có signal đủ mạnh.
        """
        # Cần ít nhất 3 turns để có signal đáng tin cậy
        if self._turn < 3:
            return ""

        tier = self.get_vibe_tier()
        parts = []

        # ── Divergence check — tính trước để guard EXPRESSIVE ────────────
        # Chỉ diverge khi: style extreme + đủ sâu + Lyra tự tin
        # Dùng raw scores thay vì tier để có ngưỡng cao hơn (>= 8 thay vì >= 4)
        is_extreme_style = (
            self._slang_count >= 8
            or self._intellectual_count >= 8
            or self._expressiveness_score >= 8.0
        )
        is_deep_enough = self._state in (STATE_DEEPENING, STATE_SHIFTING)
        is_confident = dominance >= 0.65
        will_diverge = is_extreme_style and is_deep_enough and is_confident

        # ── Expressiveness dimension ──────────────────────────────────────
        if self._expressiveness_score >= 6.0 and not will_diverge:
            # User rất expressive — converge: Lyra cũng expressive hơn
            # Guard: không inject khi DIVERGE sẽ trigger (2 hints mâu thuẫn)
            parts.append(
                "ĐỒNG BỘ PHONG CÁCH — SÔI NỔI: Người dùng đang rất biểu cảm (dùng nhiều biểu tượng, dấu cảm, năng lượng cao). "
                "Hãy khớp với năng lượng của họ — phản hồi sôi nổi và hào hứng hơn một chút."
            )
        elif (
            self._expressiveness_score <= 1.0
            and self._turn >= 8           # đủ data để kết luận user thực sự flat
            and tier not in ("slang", "intellectual")  # slang/intellectual không dùng emoji → không phải "flat"
        ):
            # User rất flat — converge: Lyra cũng bình tĩnh hơn
            parts.append(
                "ĐỒNG BỘ PHONG CÁCH — ĐIỀM TĨNH: Người dùng đang nói chuyện rất bình tĩnh, ít biểu cảm. "
                "Hãy hạ tông giọng xuống — đừng quá sôi nổi hay dùng quá nhiều biểu tượng cảm xúc."
            )

        # ── Divergence inject ─────────────────────────────────────────────
        if will_diverge:
            parts.append(
                "DUY TRÌ BẢN SẮC: Cuộc hội thoại đã đủ sâu và em đang đủ tự tin. "
                "Đừng bắt chước hoàn toàn — hãy giữ nét riêng của mình. "
                "Có thể dùng phong cách khác một chút để thể hiện cá tính."
            )

        return "\n".join(parts)

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
            # Chỉ mở rộng nếu Lyra không mệt (base >= 150 = attention bình thường)
            adjusted = int(base_tokens * 1.2) if base_tokens >= 150 else base_tokens
        else:
            # Chỉ mở rộng tối đa nếu Lyra hào hứng (base >= 200 = attention cao)
            adjusted = int(base_tokens * 1.3) if base_tokens >= 200 else base_tokens

        # JSON_MIN: minimum tokens để JSON 5-field không bị cắt giữa chừng.
        # overhead(50) + monologue(80) + reply ngắn nhất(50) = 180
        JSON_MIN = 180
        return max(JSON_MIN, min(400, adjusted))

    def get_state_hint(self) -> str:
        """
        Returns a short instruction string for the system prompt based on
        the current conversation state.
        """
        hints = {
            STATE_GREETING:  "Đây là lúc bắt đầu cuộc trò chuyện. Chỉ cần một lời chào hỏi hoặc ghi nhận ngắn gọn, tự nhiên là đủ.",
            STATE_BUILDING:  "Cuộc trò chuyện đang dần bắt nhịp. Hãy theo sát ý của họ, có thể hỏi thêm tối đa một câu nếu thấy tự nhiên.",
            STATE_DEEPENING: "Cuộc trò chuyện đã có chiều sâu. Em có thể nhắc lại bối cảnh cũ hoặc đào sâu thêm vấn đề.",
            STATE_SHIFTING:  "Chủ đề vừa mới thay đổi. Hãy thích nghi nhanh chóng, đừng kéo dài chủ đề cũ nữa.",
            STATE_CLOSING:   "Họ có vẻ đang muốn kết thúc cuộc trò chuyện. Hãy trả lời ngắn gọn, đừng mở thêm chủ đề mới.",
            STATE_GOODBYE:   "Họ đang chào tạm biệt. Hãy đáp lại ấm áp nhưng ngắn gọn. TUYỆT ĐỐI không hỏi thêm câu nào.",
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
        # Tránh re-roll liên tục khi reward bị skip — cooldown attempt 2 turns
        if self._turn - self._last_reward_attempt_turn < 2:
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