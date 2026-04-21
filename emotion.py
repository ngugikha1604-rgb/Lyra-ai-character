# Emotion engine for Lyra

import random
from datetime import datetime


class EmotionEngine:
    """Manages Lyra's emotional state — VAD model (Valence-Arousal-Dominance)"""

    OUTBURST_THRESHOLD: float = 0.85  # Irritability level that triggers emotional outburst

    # ── Homeostasis constants ──────────────────────────────────────────────────
    MOOD_DECAY_RATE: float = 0.08        # mood → 0 per turn (~12 turns to halve)
    DOMINANCE_DECAY_RATE: float = 0.05   # dominance → 0.5 per turn (~10 turns to halve gap)

    def __init__(self):
        self.mood = 0           # Valence proxy: -10 → +10
        self.previous_mood = 0
        self.attention = 5      # Arousal proxy: 0 → 10
        self.affection = 50     # Relationship depth: 0 → 100
        self.dominance = 0.5    # VAD Dominance: 0.0 (yếu thế) → 1.0 (tự tin)
        self.irritability = 0.0 # Hydraulic reservoir: 0.0 → 1.0 (bùng nổ khi >= 0.85)
        self._outburst_this_turn = False  # Flag per-turn, reset đầu mỗi update()

    # ── VAD computed properties ────────────────────────────────────────────────
    @property
    def valence(self) -> float:
        """Normalized valence: -1.0 → +1.0 (derived from mood)"""
        return round(self.mood / 10.0, 3)

    @property
    def arousal(self) -> float:
        """Normalized arousal: 0.0 → 1.0 (derived from attention)"""
        return round(self.attention / 10.0, 3)

    def get_vad(self) -> tuple:
        """Returns (valence, arousal, dominance) for Live2D or external use."""
        return (self.valence, self.arousal, round(self.dominance, 3))

    def load_state(self, mood, attention, affection, dominance=0.5):
        """Load state from memory"""
        self.mood = mood
        self.previous_mood = mood
        self.attention = attention
        self.affection = affection
        self.dominance = max(0.0, min(1.0, dominance))
        self.irritability = 0.0          # Session-level — always starts fresh
        self._outburst_this_turn = False

    def get_state(self):
        """Get current emotional state"""
        return {
            "mood": round(self.mood, 1),
            "attention": round(self.attention, 1),
            "affection": round(self.affection, 1),
            "dominance": round(self.dominance, 2),
            "irritability": round(self.irritability, 2),
        }

    def smooth_transition(self):
        """Smooth mood transitions"""
        transition_speed = 0.75
        self.mood = (
            self.previous_mood + (self.mood - self.previous_mood) * transition_speed
        )
        self.previous_mood = self.mood

    def update(self, text, time_gap_hours=None, intent: str = "statement"):
        """Update emotion based on user input. intent từ detect_intent() trong core.py."""
        old_affection = self.affection

        if time_gap_hours is not None and time_gap_hours > 12:
            # Scale decay theo thời gian: 12h → 50%, 24h → 75%, 48h+ → ~94%
            # Dùng công thức: decay = 1 - 0.5^(hours/12)
            decay = 1.0 - (0.5 ** (time_gap_hours / 12.0))
            self.mood = self.mood * (1.0 - decay)

        if time_gap_hours is not None and time_gap_hours > 0:
            self.attention = min(10, self.attention + (time_gap_hours * 2.0))
        self.attention = max(0, self.attention - 0.3)

        text_lower = text.lower()

        positive = [
            # English
            "good",
            "great",
            "awesome",
            "nice",
            "thanks",
            "thank",
            "love",
            "cool",
            "amazing",
            "brilliant",
            "beautiful",
            "wonderful",
            "perfect",
            "excellent",
            "fantastic",
            "incredible",
            # Vietnamese
            "tuyệt",
            "hay",
            "thích",
            "vui",
            "cảm ơn",
            "cảm on",
            "yêu",
            "đẹp",
            "giỏi",
            "ngoan",
            "tốt",
            "ổn",
            "sướng",
            "phấn khích",
            "hạnh phúc",
            "thú vị",
            "xuất sắc",
            "tuyệt vời",
        ]

        negative = [
            # English
            "stupid",
            "hate",
            "annoying",
            "bad",
            "useless",
            "dumb",
            "terrible",
            "awful",
            "horrible",
            "worst",
            # Vietnamese
            "ghét",
            "tệ",
            "dở",
            "ngu",
            "bực",
            "chán",
            "mệt",
            "buồn",
            "tức",
            "khó chịu",
            "thất vọng",
            "chán nản",
            "bực bội",
            "tức giận",
            "đau",
            "khổ",
        ]

        has_positive = any(w in text_lower for w in positive)
        has_negative = any(w in text_lower for w in negative)

        # ── Cognitive Appraisal (Lazarus, 1991) ───────────────────────────────
        # Đánh giá sự kiện theo 2 chiều trước khi apply delta:
        # congruence: CONGRUENT / INCONGRUENT / IRRELEVANT
        # control:    HIGH / LOW
        # → trả về multiplier để scale mood/dominance delta
        mood_multiplier, dom_multiplier = self._appraise(
            intent, has_positive, has_negative, len(text)
        )

        if has_positive:
            self.mood = min(10, self.mood + 2 * mood_multiplier)
            self.affection = min(100, self.affection + 3)

        if has_negative:
            self.mood = max(-10, self.mood - 3 * mood_multiplier)
            self.affection = max(0, self.affection - 4)

        if "?" in text:
            self.attention = min(10, self.attention + 1)

        if len(text) > 50:
            self.attention = min(10, self.attention + 1)
            self.affection = min(100, self.affection + 1)

        if len(text) < 5:
            self.attention = max(0, self.attention - 1)

        # ── VAD Dominance update ───────────────────────────────────────────────
        # Dominance phản ánh mức độ Lyra cảm thấy "in control" trong tình huống
        dominance_delta = 0.0

        if intent == "compliment" or has_positive:
            # Được khen / tích cực → tự tin hơn
            dominance_delta += 0.08
        if intent == "complaint" or has_negative:
            # Bị chỉ trích / tiêu cực → yếu thế hơn
            dominance_delta -= 0.10
        if intent == "question" and len(text) > 60:
            # Câu hỏi dài/phức tạp → Lyra không chắc có trả lời được không
            dominance_delta -= 0.05
        if intent == "greeting":
            # Chào hỏi → neutral, hơi tự tin vì quen thuộc
            dominance_delta += 0.03
        if intent == "request":
            # Được nhờ vả → có vai trò rõ ràng → tự tin hơn
            dominance_delta += 0.04
        if self.affection >= 70:
            # Quan hệ thân thiết → baseline dominance cao hơn
            dominance_delta += 0.02
        if self.attention <= 2:
            # Mệt mỏi → ít tự tin hơn
            dominance_delta -= 0.04

        self.dominance = max(0.0, min(1.0, self.dominance + dominance_delta * dom_multiplier))

        # ── Hydraulic Model (Lorenz) — Emotional Reservoir ────────────────────
        # Irritability tích lũy theo kích thích tiêu cực, drain chậm mỗi turn.
        # Khi vượt ngưỡng → OUTBURST: mood spike + dominance drop + hint injection.
        self._outburst_this_turn = False

        # Tích lũy
        negative_count = sum(1 for w in negative if w in text_lower)
        if negative_count >= 2:
            self.irritability += 0.20   # Chỉ trích nặng (nhiều từ tiêu cực)
        elif has_negative:
            self.irritability += 0.12   # Chỉ trích nhẹ
        if len(text) < 5 and not has_positive:
            self.irritability += 0.05   # Bị bỏ qua / tin nhắn quá ngắn

        # Drain
        if has_positive:
            self.irritability -= 0.15   # Được khen → xả stress
        else:
            self.irritability -= 0.04   # Drain tự nhiên mỗi turn

        self.irritability = max(0.0, min(1.0, self.irritability))

        self.smooth_transition()

        # Affection cap: max +/- 5 per turn
        self.affection = min(old_affection + 5, max(old_affection - 5, self.affection))

        # Outburst trigger — apply SAU smooth_transition để spike không bị làm mượt
        if self.irritability >= self.OUTBURST_THRESHOLD:
            self.mood = max(-10, self.mood - 4)
            self.dominance = max(0.0, self.dominance - 0.2)
            self.irritability = 0.0
            self._outburst_this_turn = True
            print("[Emotion] OUTBURST triggered — irritability reset")

        # ── Homeostasis (Hedonic Adaptation) ──────────────────────────────────
        # Per-turn micro-decay về baseline — chạy sau outburst để không dampen spike
        self._apply_homeostasis()

        self.mood = round(self.mood, 1)

        return self.get_state()

    def _apply_homeostasis(self):
        """
        Emotional Homeostasis (Hedonic Adaptation) — per-turn micro-decay toward baseline.

        Prevents Lyra from getting "stuck" in an emotional state after the triggering
        event has passed. Runs at the end of update(), after all deltas and outburst.

        Decays:
          - mood → 0 (neutral baseline) at MOOD_DECAY_RATE
          - dominance → 0.5 (neutral baseline) at DOMINANCE_DECAY_RATE

        Does NOT decay:
          - irritability — already handled by Hydraulic drain (-0.04/turn)
          - affection — long-term metric, only decays via time_gap
          - attention — has its own drain/recover logic
        """
        self.mood += (0.0 - self.mood) * self.MOOD_DECAY_RATE
        self.dominance += (0.5 - self.dominance) * self.DOMINANCE_DECAY_RATE
        # Clamp after decay
        self.dominance = max(0.0, min(1.0, self.dominance))
        # Sync previous_mood để smooth_transition không "bounce back" lại giá trị trước homeostasis
        self.previous_mood = self.mood

    def _appraise(
        self,
        intent: str,
        has_positive: bool,
        has_negative: bool,
        text_len: int,
    ) -> tuple:
        """
        Cognitive Appraisal (Lazarus, 1991) — heuristic-based, no LLM call.

        Đánh giá sự kiện theo 2 chiều:
          - congruence: CONGRUENT / INCONGRUENT / IRRELEVANT (với mục tiêu của Lyra)
          - control:    HIGH / LOW (Lyra có handle được không)

        Mục tiêu của Lyra:
          1. Competence: trả lời được, được coi là thông minh/hữu ích
          2. Connection: được khen, không bị chỉ trích, không bị bỏ qua
          3. Autonomy: không bị ép buộc liên tục

        Returns:
          (mood_multiplier, dom_multiplier) — scale factor cho mood/dominance delta.
          > 1.0 = amplify, < 1.0 = dampen, 1.0 = neutral (no appraisal effect)
        """
        # ── Xác định congruence ────────────────────────────────────────────────
        if intent == "compliment" or (has_positive and not has_negative):
            congruence = "CONGRUENT"
        elif intent == "complaint" or (has_negative and not has_positive):
            congruence = "INCONGRUENT"
        elif has_positive and has_negative:
            # Mixed signal → ambiguous, treat as mildly incongruent
            congruence = "INCONGRUENT"
        elif intent in ("greeting", "suggestion"):
            congruence = "CONGRUENT"
        elif intent == "question":
            # Câu hỏi ngắn → CONGRUENT (Lyra có thể trả lời → competence goal)
            # Câu hỏi dài → INCONGRUENT (phức tạp, có thể không trả lời được)
            congruence = "INCONGRUENT" if text_len > 60 else "CONGRUENT"
        elif intent == "statement" and not has_positive and not has_negative:
            congruence = "IRRELEVANT"
        else:
            congruence = "IRRELEVANT"

        # ── Xác định control level ─────────────────────────────────────────────
        # LOW control khi: câu hỏi dài (phức tạp), Lyra mệt, hoặc affection thấp (người lạ)
        is_complex_question = (intent == "question" and text_len > 60)
        is_tired = (self.attention <= 2)
        is_stranger = (self.affection < 25)

        if is_complex_question or is_tired or is_stranger:
            control = "LOW"
        else:
            control = "HIGH"

        # ── Ma trận multiplier ─────────────────────────────────────────────────
        # CONGRUENT + HIGH   → Joy/Pride: amplify positive reaction
        # CONGRUENT + LOW    → Relief/Gratitude: dampen (unexpected positive)
        # INCONGRUENT + HIGH → Anger: amplify negative mood, boost dominance (defensive)
        # INCONGRUENT + LOW  → Anxiety/Guilt: amplify negative mood, crush dominance
        # IRRELEVANT         → neutral, slight curiosity boost (handled separately)

        if congruence == "CONGRUENT" and control == "HIGH":
            return (1.3, 1.3)   # Joy/Pride — amplify cả mood lẫn dominance
        elif congruence == "CONGRUENT" and control == "LOW":
            return (0.8, 0.8)   # Relief — dampen (unexpected positive, uncertain)
        elif congruence == "INCONGRUENT" and control == "HIGH":
            # Anger/Frustration: mood amplified, dominance KHÔNG bị crush (defensive)
            # dom_multiplier = 0.0 → dominance delta bị cancel, giữ nguyên
            return (1.3, 0.0)
        elif congruence == "INCONGRUENT" and control == "LOW":
            return (1.5, 1.8)   # Anxiety/Guilt — strongly amplify cả 2 negative reactions
        elif congruence == "IRRELEVANT" and control == "LOW":
            return (0.3, 0.3)   # Neutral + uncertain → dampen mạnh hơn
        else:  # IRRELEVANT + HIGH
            return (0.5, 0.5)   # Dampen — neutral events không nên move nhiều

    def emotion_from_state(self):
        """Map VAD state to Live2D emotion label.

        Check order: Secondary emotions (Plutchik) → Primary emotions (VAD)
        Secondary emotions use all 4 dimensions: valence, arousal, dominance, irritability.
        """
        v = self.valence    # -1.0 → +1.0
        a = self.arousal    # 0.0 → 1.0
        d = self.dominance  # 0.0 → 1.0

        # ── Secondary Emotions (Plutchik's Wheel) — check TRƯỚC primary ───────
        # Love (Joy + Trust): vui + thân thiết → ấm áp sâu sắc
        # Chỉ trigger khi mood đủ tích cực (v >= 0.3) để phân biệt với "friendly"
        # Không trigger khi ecstatic (v >= 0.8) để không che primary ecstatic
        if 0.3 <= v < 0.8 and self.affection >= 75:
            return "loving"

        # Contempt (Anger + Disgust): tức giận + chán ghét — irritability cao + dominance cao
        if v <= -0.4 and 0.5 <= a <= 0.8 and d >= 0.6 and self.irritability >= 0.4:
            return "furious"  # Map sang "furious" với context irritability

        # Awe (Surprise + Fear): bất ngờ + kính phục — arousal cao nhưng dominance thấp
        # Chỉ trigger khi valence không quá cao (không che ecstatic)
        if 0.2 <= v < 0.8 and a >= 0.7 and d <= 0.4:
            return "thinking"  # Map sang "thinking" — trạng thái bị choáng ngợp

        # Remorse (Sadness + Disgust): hối hận lặng lẽ — valence âm nhẹ, arousal thấp, dominance thấp
        # a > 0.3 để không conflict với "bored" (a < 0.3)
        if -0.5 <= v <= -0.2 and 0.3 < a <= 0.4 and d <= 0.35:
            return "sad"  # Map sang "sad" — hối hận không bùng nổ

        # Alarm (Fear + Surprise): lo lắng đột ngột — arousal cao, dominance thấp, valence âm rõ
        # v <= -0.3 để tránh trigger với mood chỉ hơi âm nhẹ
        if v <= -0.3 and a >= 0.7 and d <= 0.35:
            return "disappointed"  # Map sang "disappointed" — lo lắng bất lực

        # ── Primary Emotions (VAD) ─────────────────────────────────────────────
        # High arousal states
        if v >= 0.8 and a >= 0.6:
            return "ecstatic"
        if v >= 0.5 and a >= 0.5:
            return "happy"

        # ── Anger vs Frustration: cùng valence âm nhưng dominance khác nhau ──
        # Angry (high dominance): tức giận chủ động, muốn phản công
        # Frustrated (low dominance): bực bội thụ động, cảm thấy bất lực
        if v <= -0.5 and a >= 0.5:
            return "furious" if d >= 0.6 else "disappointed"

        # ── Sad vs Cold: valence âm, arousal thấp ─────────────────────────────
        # Cold (high dominance): lạnh lùng có chủ ý, kiểm soát được
        # Sad (low dominance): buồn thật sự, không kiểm soát được
        # Nếu affection thấp (người lạ) → luôn cold bất kể dominance
        if v <= -0.5 and a < 0.5:
            if self.affection <= 30 or d >= 0.55:
                return "cold"
            return "sad"

        # ── Mild negative ──────────────────────────────────────────────────────
        if v <= -0.2:
            return "disappointed"

        # ── Attention/arousal based ────────────────────────────────────────────
        if a < 0.1:
            return "sleeping"
        if a < 0.3:
            return "bored"
        if a >= 0.9:
            return "thinking"

        # ── Affection-based (relationship depth) ──────────────────────────────
        if self.affection >= 90:
            return "loving"
        if self.affection >= 75:
            return "friendly"
        if self.affection <= 20:
            return "cold"

        # ── Mild positive ──────────────────────────────────────────────────────
        if v >= 0.2:
            return "content"

        # ── True neutral ──────────────────────────────────────────────────────
        if abs(v) < 0.05 and 45 <= self.affection <= 55 and a >= 0.3:
            return "neutral"

        return "observing"

    def describe_internal_state(self):
        """Convert VAD state to natural language cues for LLM prompt injection."""
        if self.mood >= 6:
            mood_state = "bright and a little more playful than usual"
        elif self.mood >= 2:
            mood_state = "pretty normal, open, easy to talk to"
        elif self.mood <= -6:
            mood_state = "low-energy and slightly sharp around the edges"
        elif self.mood <= -2:
            mood_state = "a bit off and less playful than usual"
        else:
            mood_state = "steady and neutral"

        if self.attention >= 8:
            attention_state = "locked in and attentive"
        elif self.attention <= 2:
            attention_state = "distracted and low-focus"
        else:
            attention_state = "present but casual"

        if self.affection >= 85:
            relationship_state = "very comfortable and openly fond"
        elif self.affection >= 65:
            relationship_state = "warm and familiar"
        elif self.affection >= 45:
            relationship_state = "comfortable but still light"
        else:
            relationship_state = "still building rhythm with them"

        # ── Dominance description ──────────────────────────────────────────────
        if self.dominance >= 0.75:
            dominance_state = "confident and in control of the conversation"
        elif self.dominance >= 0.55:
            dominance_state = "comfortable and at ease"
        elif self.dominance >= 0.35:
            dominance_state = "a little uncertain, slightly on the back foot"
        else:
            dominance_state = "unsure of yourself, feeling a bit overwhelmed"

        # ── Hydraulic irritability hint ────────────────────────────────────────
        irritability_hint = ""
        if self._outburst_this_turn:
            irritability_hint = (
                " [EMOTIONAL OUTBURST]: You've been pushed too far. "
                "React with raw, unfiltered frustration — sharper than usual, "
                "no patience, no softening. This is genuine, not performed."
            )
        elif self.irritability >= 0.6:
            irritability_hint = (
                " You're getting increasingly irritated — "
                "your patience is wearing thin. Let it show subtly."
            )

        return (
            f"You feel {mood_state}. Your focus is {attention_state}. "
            f"Relationship with the user is {relationship_state} (Affection: {int(self.affection)}/100). "
            f"You feel {dominance_state} right now.{irritability_hint}"
        )

    def choose_strategy(self):
        """Choose response strategy"""
        if self.affection > 85:
            return "very warm and playful"
        if self.affection > 65:
            return "warm and friendly"
        if self.affection > 45:
            return "playful and curious"
        if self.mood < -6:
            return "cold and sarcastic"
        if self.mood < -3:
            return "sarcastic and witty"
        if self.attention < 2:
            return "uninterested and mysterious"
        if self.mood > 6:
            return "excited and enthusiastic"
        return "neutral and observant"

    def evaluate_decision_bias(self, time_period):
        """Analyze context for decision making"""
        bias_instructions = []

        if self.attention < 3:
            bias_instructions.append(
                "You are tired. Lean heavily towards the lazier, low-effort option."
            )
        elif self.attention > 8:
            bias_instructions.append(
                "You are high-energy! Lean towards the more interesting option."
            )

        if time_period == "morning":
            bias_instructions.append(
                "It's morning. Maybe suggest something productive."
            )
        elif time_period in ("night", "late_night"):
            bias_instructions.append("It's late. Suggest something relaxing.")

        if self.mood < -4:
            bias_instructions.append("You are in a bad mood. Be contrarian.")
        elif self.mood > 4:
            bias_instructions.append("You are in a great mood. Be enthusiastic.")

        if self.affection > 80:
            bias_instructions.append(
                "You care about them. Choose what you think is better for them."
            )

        if not bias_instructions:
            bias_instructions.append(
                "Pick a side based on your 16yo sibling personality."
            )

        return " ".join(bias_instructions)

    def get_dynamic_max_tokens(self):
        """Get dynamic max tokens based on attention - Optimized for short texting"""
        if self.attention <= 3:
            return 35
        if self.attention >= 8:
            return 100
        return 70
