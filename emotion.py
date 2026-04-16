# Emotion engine for Lyra

import random
from datetime import datetime


class EmotionEngine:
    """Manages Lyra's emotional state"""

    def __init__(self):
        self.mood = 0
        self.previous_mood = 0
        self.attention = 5
        self.affection = 50

    def load_state(self, mood, attention, affection):
        """Load state from memory"""
        self.mood = mood
        self.previous_mood = mood
        self.attention = attention
        self.affection = affection

    def get_state(self):
        """Get current emotional state"""
        return {
            "mood": round(self.mood, 1),
            "attention": round(self.attention, 1),
            "affection": round(self.affection, 1),
        }

    def smooth_transition(self):
        """Smooth mood transitions"""
        transition_speed = 0.75
        self.mood = (
            self.previous_mood + (self.mood - self.previous_mood) * transition_speed
        )
        self.previous_mood = self.mood

    def update(self, text, time_gap_hours=None):
        """Update emotion based on user input"""
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
        ]

        negative = [
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
        ]

        if any(w in text_lower for w in positive):
            self.mood = min(10, self.mood + 2)
            self.affection = min(100, self.affection + 3)

        if any(w in text_lower for w in negative):
            self.mood = max(-10, self.mood - 3)
            self.affection = max(0, self.affection - 4)

        if "?" in text:
            self.attention = min(10, self.attention + 1)

        if len(text) > 50:
            self.attention = min(10, self.attention + 1)
            self.affection = min(100, self.affection + 1)

        if len(text) < 5:
            self.attention = max(0, self.attention - 1)

        self.smooth_transition()

        # Affection cap: max +/- 5 per turn
        self.affection = min(old_affection + 5, max(old_affection - 5, self.affection))

        self.mood = round(self.mood, 1)

        return self.get_state()

    def emotion_from_state(self):
        """Map state to Live2D emotion"""
        if self.mood >= 8:
            return "ecstatic"
        if self.mood >= 5:
            return "happy"
        if self.mood >= 2:
            return "content"
        if self.mood <= -8:
            return "furious"
        if self.mood <= -5:
            return "sad"
        if self.mood <= -2:
            return "disappointed"
        if self.attention < 1:
            return "sleeping"
        if self.attention < 3:
            return "bored"
        if self.attention >= 9:
            return "thinking"
        if self.affection >= 90:
            return "loving"
        if self.affection >= 75:
            return "friendly"
        if self.affection <= 20:
            return "cold"
        if abs(self.mood) < 0.5 and 45 <= self.affection <= 55 and self.attention >= 3:
            return "neutral"
        return "observing"

    def describe_internal_state(self):
        """Convert to natural language cues"""
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

        return (
            f"You feel {mood_state}. Your focus is {attention_state}. "
            f"Relationship with the user is {relationship_state} (Affection: {int(self.affection)}/100)."
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
