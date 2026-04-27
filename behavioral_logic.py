import re
import random
from typing import Tuple, Optional
from prompts import ILLOCUTION_HINTS, SELF_DISCLOSURE_TEMPLATES, MILESTONE_MSGS, AFFECTION_MILESTONES

class BehavioralMixin:
    """
    Mixin for Lyra's behavioral and psychological logic.
    Handles speech acts, self-disclosure, interaction tweaks, and intent detection.
    """
    def infer_user_signal(self, text: str) -> str:
        """Heuristically infers the user's current 'signal' or vibe."""
        mood = self.detect_user_mood(text)
        intent = self.detect_intent(text)
        
        if mood == "frustrated": return "User seems annoyed or impatient."
        if mood == "sad": return "User seems down or needs comfort."
        if mood == "stressed": return "User is under pressure."
        if mood == "excited": return "User is high energy and happy."
        
        if intent == "question": return "User is curious and seeking information."
        if intent == "complaint": return "User is criticizing or expressing dissatisfaction."
        if intent == "request": return "User wants you to do something."
        
        return "User is making a casual statement."

    def clean_reply(self, text: str) -> str:
        """Cleans AI response from meta-talk, quotes, and artifacts."""
        if not text: return ""
        # Remove quotes
        text = text.strip().strip('"').strip("'")
        # Remove common meta-prefixes
        text = re.sub(r"^(Lyra:|Assistant:|AI:)", "", text, flags=re.I).strip()
        # Remove potential markdown
        text = re.sub(r"```.*?```", "", text, flags=re.S).strip()
        return text

    # ── Compiled Patterns ──────────────────────────────────────────────────────
    _INTRO_PATTERNS = [
        re.compile(r"(my name is|i'm called|call me|i am [a-z]+|i'm [a-z]+)", re.I),
        re.compile(r"(tên (em|anh|tôi|mình) là|tên (em|anh|tôi|mình)|gọi (em|anh|tôi|mình) là)", re.I),
    ]
    _SUGGESTIVE_PATTERN = re.compile(r"\b(nhé|nha|đi)\b", re.I)
    _QUESTION_WORDS = {"what", "how", "why", "when", "where", "who", "gì", "sao", "tại sao", "bao giờ"}
    _VN_CONFIRM_PATTERN = re.compile(r"\b(nhỉ|hở|phải không|không nhỉ|đúng không)\b", re.I)
    _COMPLIMENT_WORDS = {"love", "amazing", "beautiful", "awesome", "great", "nice", "thích", "tuyệt", "đẹp", "ngoan", "giỏi"}
    _COMPLAINT_WORDS = {"hate", "bad", "terrible", "awful", "stupid", "useless", "angry", "ghét", "tệ", "dở", "ngu", "bực", "chán"}
    _REQUEST_PHRASES = ["can you", "could you", "giúp", "làm ơn"]
    _REQUEST_WORDS = {"please", "help"}
    _CHOICE_PATTERNS = [
        re.compile(r"\b(or|hay|hoặc)\b", re.I),
        re.compile(r"nào (nhỉ|đây|hơn)", re.I),
        re.compile(r"(cái nào|bên nào|chọn gì|chọn cái|nên chọn)", re.I)
    ]
    _SARCASM_PATTERN = re.compile(r"\b(đấy|cơ mà|thế mà|mà thôi)\b", re.I)
    _STRESS_WORDS = {"stressed", "tired", "exhausted", "overwhelmed", "can't sleep", "can't focus", "so much work", "mệt", "kiệt sức", "áp lực", "đuối", "stress"}
    _SAD_WORDS = {"sad", "depressed", "lonely", "miss", "crying", "unhappy", "heartbroken", "hurt", "buồn", "cô đơn", "khóc", "nhớ", "thất vọng"}
    _EXCITED_WORDS = {"excited", "happy", "so good", "amazing", "can't wait", "yay", "woohoo", "finally", "vui", "tuyệt", "sướng", "phấn khích", "quá"}
    _BORED_WORDS = {"bored", "nothing to do", "boring", "slow day", "so bored", "chán", "nhạt"}
    _ANGRY_WORDS = {"angry", "frustrated", "annoyed", "pissed", "ugh", "argh", "so annoying", "bực", "tức", "ghét", "khó chịu", "tức quá"}
    _ANXIOUS_WORDS = {"nervous", "anxious", "worried", "scared", "fear", "anxiety", "panic", "lo", "sợ", "hồi hộp", "căng thẳng"}
    
    _EXPRESSIVE_SIGNALS = {
        "mệt", "mệt quá", "mệt rồi", "buồn", "chán", "stress", "áp lực", "tệ quá", "tệ thật", "khó chịu",
        "bực", "tức", "đau", "khổ", "cô đơn", "nhớ anh", "nhớ em", "nhớ bạn", "nhớ nhà", "thất vọng",
        "nản", "chán nản", "vui quá", "vui ghê", "sướng", "phấn khích", "hạnh phúc", "tuyệt quá",
        "hay quá", "thích quá", "so tired", "so sad", "so happy", "so excited", "feel like", "i'm tired",
        "i'm sad", "i'm happy", "i feel"
    }
    _EXPRESSIVE_ENDINGS = re.compile(r"(quá|ghê|thật|vậy|luôn|á|ơi)\s*[.!]*$", re.I)
    _COMMISSIVE_SIGNALS = {
        "mình sẽ", "tôi sẽ", "em sẽ", "anh sẽ", "mình sẽ cố", "mình sẽ thử", "mình sẽ làm", "lần này mình",
        "lần sau mình", "từ nay mình", "mình quyết định", "mình đã quyết", "i will", "i'll", "i'm going to",
        "i plan to", "gonna", "i promise", "i'll try"
    }
    _ASSERTIVE_SIGNALS = {
        "xong rồi", "làm xong", "hoàn thành", "mình vừa", "vừa xong", "vừa làm", "vừa giải", "mình đã xong",
        "mình đã làm được", "mình đã giải được", "đã làm được", "đã xong", "đã giải được", "cuối cùng",
        "cuối cùng rồi", "finally", "i just", "i did it", "i finished", "i completed", "done!", "finished!", "got it!"
    }
    _DECLARATIVE_SIGNALS = {
        "thôi kệ", "kệ đi", "thôi vậy", "vậy là xong", "mình quyết rồi", "quyết định rồi", "không cần nữa",
        "forget it", "never mind", "that's it", "it's decided", "i've decided", "i made up my mind"
    }

    def detect_intent(self, text: str) -> str:
        """
        Heuristic-based intent detection.
        Returns: "question" | "introduction" | "greeting" | "suggestion" | "compliment" | "complaint" | "request" | "choice" | "statement"
        """
        text_lower = text.lower()

        # Introduction
        if any(p.search(text_lower) for p in self._INTRO_PATTERNS):
            return "introduction"

        # Greeting detection (EN + VN)
        greeting_words = ["hi", "hello", "hey", "sup", "chào", "hé lô", "alo"]
        if any(word in text_lower for word in greeting_words):
            return "greeting"

        # VN suggestive
        if self._SUGGESTIVE_PATTERN.search(text_lower) and not text.strip().endswith("?"):
            return "suggestion"

        is_question = text.strip().endswith("?")
        has_question_word = any(word in text_lower.split() for word in self._QUESTION_WORDS)
        has_vn_confirm = self._VN_CONFIRM_PATTERN.search(text_lower)

        if is_question or has_question_word or has_vn_confirm:
            return "question"

        if any(word in text_lower for word in self._COMPLIMENT_WORDS):
            return "compliment"

        if any(word in text_lower for word in self._COMPLAINT_WORDS):
            return "complaint"

        if any(phrase in text_lower for phrase in self._REQUEST_PHRASES) or any(word in text_lower.split() for word in self._REQUEST_WORDS):
            return "request"

        if any(p.search(text_lower) for p in self._CHOICE_PATTERNS) and (
            text.strip().endswith("?") or any(w in text_lower for w in ["nhỉ", "đây", "nào", "gì"])
        ):
            return "choice"

        return "statement"

    def detect_user_mood(self, text: str) -> str:
        """Simple keyword-based user mood detection"""
        text_lower = text.lower()

        # VN sarcasm/irritation
        if len(text.strip()) < 40 and self._SARCASM_PATTERN.search(text_lower):
            if any(w in text_lower for w in ["gì", "đâu", "sao", "không", "chẳng"]):
                return "frustrated"

        if any(w in text_lower for w in self._STRESS_WORDS): return "stressed"
        if any(w in text_lower for w in self._SAD_WORDS): return "sad"
        if any(w in text_lower for w in self._EXCITED_WORDS): return "excited"
        if any(w in text_lower for w in self._BORED_WORDS): return "bored"
        if any(w in text_lower for w in self._ANGRY_WORDS): return "frustrated"
        if any(w in text_lower for w in self._ANXIOUS_WORDS): return "anxious"

        if text_lower.strip().endswith("ạ"):
            return "polite"
        if text.count("...") >= 2:
            return "down_or_tired"
        if text.count("!") >= 3:
            return "excited"
        if text.isupper() and len(text) > 5:
            return "frustrated"
        return "neutral"

    def classify_illocution(self, text: str, intent: str) -> Tuple[str, str]:
        """
        Speech Act Classifier (Austin/Searle)
        Returns (illocution_type, perlocution_hint)
        """
        text_lower = text.lower().strip()
        text_len = len(text_lower)

        has_expressive = any(s in text_lower for s in self._EXPRESSIVE_SIGNALS)
        expressive_endings = self._EXPRESSIVE_ENDINGS.search(text_lower)
        is_question = "?" in text or intent in ("question", "choice")
        is_request = intent == "request"

        if has_expressive and not is_question and not is_request:
            return ("expressive", ILLOCUTION_HINTS["expressive"])

        if any(s in text_lower for s in self._COMMISSIVE_SIGNALS) and not is_question:
            return ("commissive", ILLOCUTION_HINTS["commissive"])

        if expressive_endings and text_len < 50 and not is_question and not is_request and intent not in ("greeting", "introduction"):
            return ("expressive", ILLOCUTION_HINTS["expressive"])

        if any(s in text_lower for s in self._ASSERTIVE_SIGNALS) and not is_question:
            return ("assertive", ILLOCUTION_HINTS["assertive"])

        if any(s in text_lower for s in self._DECLARATIVE_SIGNALS) and not is_question:
            return ("declarative", ILLOCUTION_HINTS["declarative"])

        if intent in ("question", "request", "choice"):
            return ("directive", ILLOCUTION_HINTS["directive"])

        return ("neutral", "")

    def _get_self_disclosure_hint(self, intent: str, illocution: str) -> str:
        """
        Self-Disclosure Engine (Walther — SIP Theory)
        """
        if self.emotion.affection < 50:
            return ""
        if self.emotion.irritability >= 0.4:
            return ""
        if self.turn_counter - self._last_disclosure_turn < 8:
            return ""
        if random.random() >= 0.12:
            return ""

        disclosure_type = None
        if illocution == "directive" and intent == "question":
            disclosure_type = "processing_state"
        elif self.emotion.dominance <= 0.35:
            disclosure_type = "uncertainty"
        elif illocution == "assertive" and self.emotion.affection >= 60:
            disclosure_type = "aesthetic_reaction"
        elif self.emotion.affection >= 65 and illocution in ("expressive", "assertive", "commissive"):
            disclosure_type = "preference"

        if not disclosure_type:
            return ""

        templates = SELF_DISCLOSURE_TEMPLATES.get(disclosure_type, [])
        if not templates:
            return ""

        self._last_disclosure_turn = self.turn_counter
        return random.choice(templates)

    def check_milestone(self) -> Optional[str]:
        """Checks if a relationship milestone has been reached"""
        total_messages = self.memory.memory["conversation"].get("total_messages", 0)
        affection = int(self.emotion.affection)
        milestones = self.memory.memory["relationship"].get("milestones_reached", [])
        milestone_msg = None

        for threshold, msg in MILESTONE_MSGS.items():
            key = f"msg_{threshold}"
            if total_messages >= threshold and key not in milestones:
                milestones.append(key)
                milestone_msg = msg
                break

        if not milestone_msg:
            for threshold, (key, msg) in AFFECTION_MILESTONES.items():
                if affection >= threshold and key not in milestones:
                    milestones.append(key)
                    milestone_msg = msg
                    break

        self.memory.memory["relationship"]["milestones_reached"] = milestones
        return milestone_msg

    def is_too_similar(self, response: str) -> bool:
        """Checks if the response is too similar to recent ones to avoid repetition."""
        response_lower = response.lower()[:30]
        if len(response_lower.strip()) < 8:
            return False
        for prev in self.recent_responses[-5:]:
            if response_lower == prev:
                return True
            if len(prev) >= 15 and len(response_lower) >= 15:
                if response_lower in prev or prev in response_lower:
                    return True
        return False

    _FILLER_WORDS = ["hmmm... ", "ừm... ", "à thì... ", "đợi em nghĩ tí... ", "ờ... ", "ừ thì... ", "à... ", "hmm... "]
    _FILLER_TRIGGER = re.compile(
        r"(tại sao|vì sao|như thế nào|thế nào|nghĩ gì|ý kiến|cảm thấy|giải thích|phân tích"
        r"|\bwhy\b|\bhow\b|\bwhat do you think\b|\bopinion\b|\bfeel\b|\bexplain\b)",
        re.IGNORECASE,
    )

    def _maybe_add_filler(self, reply: str, user_input: str, source_type: str) -> str:
        """Adds natural fillers based on input complexity."""
        if source_type != "owner" or not reply or reply == "...":
            return reply
        if not self._FILLER_TRIGGER.search(user_input) or random.random() >= 0.12:
            return reply

        filler = random.choice(self._FILLER_WORDS)
        if reply and reply[0].isupper():
            reply = reply[0].lower() + reply[1:]
        
        result = filler + reply
        print(f"[Dopamine] Filler word injected: '{filler.strip()}'")
        return result
