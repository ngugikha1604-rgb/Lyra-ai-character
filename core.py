import os

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

import json
import random
import requests
import re
from datetime import datetime

from config import *

from prompts import (
    BASE_PERSONALITY,
    NATURAL_BASE_PERSONALITY,
    VTUBER_BRAIN_INSTRUCTIONS,
    MEMORY_EXTRACTION_PROMPT,
    MEMORY_EXTRACT_SYSTEM,
    SUMMARIZE_PROMPT,
    MEMORY_COMPRESSION_PROMPT,
    REFLECTION_HINTS,
    RELATIONSHIP_HINTS,
    MOOD_HINTS,
    USER_MOOD_HINTS,
    INTENT_HINTS,
    PERSONA_TIERS,
    MILESTONE_MSGS,
    AFFECTION_MILESTONES,
)

from time_utils import (
    get_vietnam_time,
    get_time_period,
    calculate_time_gap,
    should_send_greeting,
    get_returning_greeting,
    get_time_context,
    get_proactive_time_flavor,
    get_weekend_context,
    get_proactive_message_situation,
)

from emotion import EmotionEngine
from memory import MemorySystem
from vbrain import parse_vbrain_response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class MiniAI:
    """Main AI engine for Lyra"""

    def __init__(self):
        self.model = DEFAULT_MODEL
        self.headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }

        self.current_vbrain = {
            "monologue": "",
            "emotion": "neutral",
            "action": "NONE",
            "reply": "",
        }
        self.messages = []
        self.recent_responses = []
        self.last_intent = None
        self._user_mood_today = None

        self.emotion = EmotionEngine()
        self.memory = MemorySystem(max_summaries=MAX_SUMMARIES)
        self.memory.load()

        self.turn_counter = self.memory.turn_counter
        self.emotion.affection = self.memory.memory.get("relationship", {}).get(
            "current_affection", 50
        )

        self.current_time = get_vietnam_time()
        self.time_period = get_time_period(self.current_time.hour)
        self.last_message_time = self.memory.memory.get("time_tracking", {}).get(
            "last_message_time"
        )
        self.time_gap_hours = calculate_time_gap(
            self.last_message_time, self.current_time
        )
        self.should_greet = should_send_greeting(
            self.time_gap_hours, self.last_message_time
        )

        self.emoji_pattern = re.compile(
            "["
            "\U0001f600-\U0001f64f"
            "\U0001f300-\U0001f5ff"
            "\U0001f680-\U0001f6ff"
            "\U0001f900-\U0001f9ff"
            "\U0001fa70-\U0001faff"
            "]",
            flags=re.UNICODE,
        )

        print("[Core] Pre-loading embedding model...")
        self.memory._get_embedding("init")

    @property
    def attention(self):
        return self.emotion.attention

    @property
    def mood(self):
        return self.emotion.mood

    @property
    def affection(self):
        return self.emotion.affection

    @property
    def memory_dict(self):
        """Direct dict access for backward compatibility"""
        return self.memory.memory

    def chat(self, user_input):
        self.current_time = get_vietnam_time()
        self.time_period = get_time_period(self.current_time.hour)
        self.time_gap_hours = calculate_time_gap(
            self.last_message_time, self.current_time
        )
        self.should_greet = should_send_greeting(
            self.time_gap_hours, self.last_message_time
        )

        self.turn_counter += 1
        intent = self.detect_intent(user_input)

        self.extract_memory(user_input, intent)
        self.emotion.update(user_input, self.time_gap_hours)

        self.summarize_history()
        if self.turn_counter % 20 == 0:
            self.memory.consolidate()

        self.last_intent = intent

        system_prompt = self.build_prompt(intent, user_input)
        composed = self.compose_user_message(user_input, intent)

        api_messages = [{"role": "system", "content": system_prompt}]

        history = self.messages[-MAX_HISTORY * 2 :]
        api_messages.extend(history)
        api_messages.append({"role": "user", "content": composed})

        dynamic_max_tokens = self.emotion.get_dynamic_max_tokens()

        data = {
            "model": self.model,
            "messages": api_messages,
            "temperature": 0.92,
            "max_tokens": dynamic_max_tokens,
        }

        reply = "..."
        regenerate_count = 0

        models_to_try = list(FALLBACK_MODELS)
        if self.model not in models_to_try:
            models_to_try = [self.model] + models_to_try

        for model_name in models_to_try:
            data["model"] = model_name
            print(f"[API] Trying model: {model_name}")
            success = False

            for attempt in range(2):
                try:
                    response = requests.post(
                        BASE_URL,
                        headers=self.headers,
                        json=data,
                        timeout=20,
                        verify=False,
                    )

                    result = response.json()
                    print(f"[API] status={response.status_code} model={model_name}")

                    if response.status_code != 200:
                        print(
                            f"[API] {model_name} failed ({response.status_code}), trying next..."
                        )
                        break

                    content = (
                        result.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "...")
                        .strip()
                    )

                    if not content or content == "...":
                        break

                    parsed = parse_vbrain_response(content)
                    reply = parsed.get("reply", "...")
                    self.current_vbrain = parsed

                    if self.is_too_similar(reply):
                        regenerate_count += 1
                        if regenerate_count < 3:
                            print(
                                f"Response too similar, regenerating... ({regenerate_count}/3)"
                            )
                            continue

                    print(f"[API] Success with: {model_name}")
                    success = True
                    break

                except Exception as e:
                    print(f"[API] ERROR {model_name} (attempt {attempt + 1}): {e}")

            if success:
                break

        reply = self.clean_reply(reply)

        milestone = self.check_milestone()
        if milestone:
            self.memory.memory["relationship"]["last_milestone_hint"] = milestone

        if self.should_greet:
            self.memory.memory["time_tracking"]["greeting_history"].append(
                {
                    "timestamp": self.current_time.isoformat(),
                    "type": "returning"
                    if self.time_gap_hours and self.time_gap_hours >= 2
                    else "first",
                    "time_period": self.time_period,
                }
            )

        self.messages.append({"role": "user", "content": user_input})
        self.messages.append({"role": "assistant", "content": reply})

        self.memory.memory["conversation"]["total_messages"] = self.turn_counter
        self.memory.memory["conversation"]["conversation_count"] += 1
        self.memory.memory["time_tracking"]["last_message_time"] = (
            self.current_time.isoformat()
        )
        self.memory.memory["time_tracking"]["time_gap_hours"] = self.time_gap_hours or 0
        self.memory.memory["relationship"]["current_affection"] = self.emotion.affection

        self.memory.save()

        emotion = self.current_vbrain.get("emotion", self.emotion.emotion_from_state())
        action = self.current_vbrain.get("action", "NONE")
        monologue = self.current_vbrain.get("monologue", "")

        return {
            "reply": reply,
            "monologue": monologue,
            "emotion": emotion,
            "action": action,
            "mood": self.emotion.mood,
            "affection": self.emotion.affection,
            "time_period": self.time_period,
            "time_gap_hours": self.time_gap_hours,
            "intent": intent,
        }

    def detect_intent(self, text):
        text_lower = text.lower()

        intro_patterns = [
            (r"(my name is|i'm called|call me|i am [a-z]+|i'm [a-z]+)", "introduction")
        ]
        for pattern, intent in intro_patterns:
            if re.search(pattern, text_lower):
                return "introduction"

        greeting_words = ["hi", "hello", "hey", "greetings", "sup"]
        if any(word in text_lower for word in greeting_words):
            return "greeting"

        if text.endswith("?") or any(
            word in text_lower.split()
            for word in ["what", "how", "why", "when", "where", "who"]
        ):
            return "question"

        if any(
            word in text_lower
            for word in [
                "love",
                "amazing",
                "beautiful",
                "awesome",
                "great",
                "wonderful",
                "nice",
            ]
        ):
            return "compliment"

        if any(
            word in text_lower
            for word in [
                "hate",
                "bad",
                "terrible",
                "awful",
                "stupid",
                "useless",
                "angry",
            ]
        ):
            return "complaint"

        if any(phrase in text_lower for phrase in ["can you", "could you"]) or any(
            word in text_lower.split()
            for word in ["please", "help"]
        ):
            return "request"

        choice_keywords = [
            r"\b(or|hay|hoặc)\b",
            r"nào (nhỉ|đây|hơn)",
            r"(cái nào|bên nào|chọn gì)",
        ]
        if any(re.search(kw, text_lower) for kw in choice_keywords) and (
            text.endswith("?")
            or any(w in text_lower for w in ["nhỉ", "đây", "nào", "gì"])
        ):
            return "choice"

        return "statement"

    def detect_user_mood(self, text):
        text_lower = text.lower()

        stress_words = [
            "stressed",
            "tired",
            "exhausted",
            "overwhelmed",
            "can't sleep",
            "can't focus",
            "so much work",
        ]
        if any(w in text_lower for w in stress_words):
            return "stressed"

        sad_words = [
            "sad",
            "depressed",
            "lonely",
            "miss",
            "crying",
            "unhappy",
            "heartbroken",
            "hurt",
        ]
        if any(w in text_lower for w in sad_words):
            return "sad"

        excited_words = [
            "excited",
            "happy",
            "so good",
            "amazing",
            "can't wait",
            "yay",
            "woohoo",
            "finally",
        ]
        if any(w in text_lower for w in excited_words):
            return "excited"

        bored_words = ["bored", "nothing to do", "boring", "slow day", "so bored"]
        if any(w in text_lower for w in bored_words):
            return "bored"

        angry_words = [
            "angry",
            "frustrated",
            "annoyed",
            "pissed",
            "ugh",
            "argh",
            "so annoying",
        ]
        if any(w in text_lower for w in angry_words):
            return "frustrated"

        anxious_words = [
            "nervous",
            "anxious",
            "worried",
            "scared",
            "fear",
            "anxiety",
            "panic",
        ]
        if any(w in text_lower for w in anxious_words):
            return "anxious"

        if text.count("...") >= 2:
            return "down_or_tired"
        if text.count("!") >= 3:
            return "excited"
        if text.isupper() and len(text) > 5:
            return "frustrated"

        if len(text.strip()) <= 5 and text_lower not in [
            "hi",
            "hey",
            "ok",
            "yes",
            "no",
            "lol",
        ]:
            return "disengaged"

        return None

    def extract_memory(self, text, intent):
        now_ts = datetime.now().isoformat()
        convo = self.memory.memory.setdefault("conversation", {})
        if not convo.get("first_chat"):
            convo["first_chat"] = now_ts
        convo["last_chat"] = now_ts

        name_patterns = [
            r"(?:my name is|i'm called|call me|my name's) ([a-zA-Z]+)",
            r"(?:you can call me) ([a-zA-Z]+)",
            r"(?:tên mình là|tên tao là|gọi mình là|tên tôi là) ([^\s,!?.]+)",
        ]
        skip_words = {
            "lyra",
            "coding",
            "python",
            "javascript",
            "game",
            "an",
            "ai",
            "the",
            "not",
            "just",
            "also",
            "really",
        }

        for pattern in name_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                if name.lower() not in skip_words and len(name) > 1:
                    self.memory.memory["user_profile"]["name"] = name
                    print(f"✓ Stored name: {name}")
                    break

        for candidate in self.memory.extract_candidates_heuristic(text):
            self.memory.buffer_candidate(
                candidate["kind"], candidate["value"], candidate.get("saliency")
            )

        if not self.memory.should_buffer(text, intent):
            return

        if not self.memory.should_flush(intent):
            return

        recent = self.messages[-4:] if len(self.messages) >= 4 else self.messages
        convo_snippet = ""
        for msg in recent:
            if isinstance(msg, dict) and "role" in msg and "content" in msg:
                role = "User" if msg["role"] == "user" else "Lyra"
                convo_snippet += f"{role}: {msg['content']}\n"
        convo_snippet += f"User: {text}"

        known = {
            "name": self.memory.memory["user_profile"].get("name", ""),
            "location": self.memory.memory["user_profile"].get("location", ""),
            "occupation": self.memory.memory["user_profile"].get("occupation", ""),
            "likes": self.memory.memory["preferences"]["likes"][:5],
            "goals": self.memory.memory["facts"].get("goals", [])[:3],
        }

        extract_prompt = [
            {
                "role": "system",
                "content": f"{MEMORY_EXTRACT_SYSTEM}\n\nAlready known (skip these): {json.dumps(known)}\nBuffered candidates: {json.dumps(self.memory.memory_buffer[-8:], ensure_ascii=False)}",
            },
            {"role": "user", "content": f"Conversation:\n{convo_snippet}"},
        ]

        try:
            response = requests.post(
                BASE_URL,
                headers=self.headers,
                json={
                    "model": self.model,
                    "messages": extract_prompt,
                    "temperature": 0.1,
                    "max_tokens": 200,
                },
                timeout=15,
                verify=False,
            )
            raw = (
                response.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            raw = re.sub(r"```json|```", "", raw).strip()
            if not raw or raw == "{}":
                return

            facts = json.loads(raw)

            profile = self.memory.memory["user_profile"]
            prefs = self.memory.memory["preferences"]
            mfacts = self.memory.memory["facts"]

            if facts.get("name") and not profile["name"]:
                profile["name"] = facts["name"]
            if facts.get("location") and not profile["location"]:
                profile["location"] = facts["location"]
            if facts.get("occupation") and not profile["occupation"]:
                profile["occupation"] = facts["occupation"]
            if facts.get("age") and not profile.get("age_range"):
                profile["age_range"] = facts["age"]

            for item in facts.get("likes", []):
                self.memory.add_item("like", item)
            for item in facts.get("dislikes", []):
                self.memory.add_item("dislike", item)
            for item in facts.get("goals", []):
                self.memory.add_item("goal", item, weight=1.4)
            for item in facts.get("topics", []):
                self.memory.add_item("topic", item)
            for item in facts.get("inside_jokes", []):
                self.memory.add_item("inside_joke", item, weight=1.5)
            for item in facts.get("relational", []):
                self.memory.add_item("relational", item, weight=1.3)

            if facts.get("mood_today"):
                self._user_mood_today = facts["mood_today"]

            extracted = [k for k in facts if facts[k] and k != "mood_today"]
            if extracted:
                print(f"✓ AI extracted: {', '.join(extracted)}")

            self.memory.memory_buffer.clear()
            self.memory._is_dirty = True

        except (json.JSONDecodeError, Exception) as e:
            print(f"[extract_memory] AI failed: {e}")

        self.memory.save()

    def summarize_history(self):
        if len(self.messages) < SUMMARY_TRIGGER:
            return

        to_summarize = self.messages[:SUMMARY_TRIGGER]

        convo_text = ""
        for msg in to_summarize:
            if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
                continue
            role = "User" if msg["role"] == "user" else "Lyra"
            convo_text += f"{role}: {msg['content']}\n"

        if not convo_text.strip():
            self.messages = self.messages[SUMMARY_TRIGGER:]
            return

        summarize_prompt = [
            {"role": "system", "content": SUMMARIZE_PROMPT},
            {
                "role": "user",
                "content": f"Summarize this conversation:\n\n{convo_text}",
            },
        ]

        try:
            response = requests.post(
                BASE_URL,
                headers=self.headers,
                json={
                    "model": self.model,
                    "messages": summarize_prompt,
                    "temperature": 0.4,
                    "max_tokens": 120,
                },
                timeout=20,
                verify=False,
            )
            summary = (
                response.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )

            if summary:
                timestamp = self.current_time.strftime("%Y-%m-%d %H:%M")
                self.save_summary_to_db(summary, timestamp)
                self.memory.add_item("episodic", summary, weight=1.2, limit=8)
                self.memory.memory["conversation"]["chat_history_summary"].append(
                    {"timestamp": timestamp, "summary": summary, "is_mega": False}
                )
                if (
                    len(self.memory.memory["conversation"]["chat_history_summary"])
                    > MAX_SUMMARIES + 1
                ):
                    self.memory.memory["conversation"]["chat_history_summary"].pop(1)

                self.messages = self.messages[SUMMARY_TRIGGER:]
                print(f"✓ Summarized {SUMMARY_TRIGGER} messages → memory")
                self.memory.save()

        except Exception as e:
            print(f"Summarize error: {e}")

    def save_summary_to_db(self, text, timestamp):
        conn = self.memory._get_db()
        if not conn:
            return

        c = conn.cursor()

        with self.memory.db_lock:
            res = c.execute("SELECT COUNT(*) FROM summaries WHERE is_mega=0").fetchone()
            count = res[0] if res else 0

        if count >= self.memory.max_summaries:
            old_summaries = c.execute(
                "SELECT id, summary, timestamp FROM summaries WHERE is_mega=0 ORDER BY id ASC"
            ).fetchall()

            old_mega = c.execute(
                "SELECT summary FROM summaries WHERE is_mega=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()

            parts = []
            if old_mega:
                parts.append(f"[Previous long-term memory]: {old_mega[0]}")
            for row in old_summaries:
                parts.append(f"[{row[1]}] {row[2]}")
            combined_text = "\n".join(parts)

            mega_text = None
            try:
                response = requests.post(
                    BASE_URL,
                    headers=self.headers,
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": MEMORY_COMPRESSION_PROMPT},
                            {
                                "role": "user",
                                "content": f"Compress these summaries:\n\n{combined_text}",
                            },
                        ],
                        "temperature": 0.3,
                        "max_tokens": 200,
                    },
                    timeout=20,
                    verify=False,
                )
                mega_text = (
                    response.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
            except Exception as e:
                print(f"[DB] Mega summary error: {e}")
                mega_text = combined_text

            if mega_text:
                with self.memory.db_lock:
                    c.execute("DELETE FROM summaries WHERE is_mega=1")
                    c.execute("DELETE FROM summaries WHERE is_mega=0")
                    c.execute(
                        "INSERT INTO summaries (summary, timestamp, is_mega) VALUES (?,?,1)",
                        (mega_text, datetime.now().strftime("%Y-%m-%d %H:%M")),
                    )
                    print(f"[DB] Compressed {count} summaries into mega summary")

        with self.memory.db_lock:
            c.execute(
                "INSERT INTO summaries (summary, timestamp, is_mega) VALUES (?,?,0)",
                (text, timestamp),
            )
            conn.commit()

    def build_prompt(self, intent, user_input):
        memory_context = self.memory.get_context(
            user_input
        ) or self.memory.get_relevant_context(user_input)
        time_context = get_time_context(self.current_time, self.time_period)

        relationship_hint = (
            RELATIONSHIP_HINTS["very_close"]
            if self.emotion.affection > 70
            else RELATIONSHIP_HINTS["building"]
            if self.emotion.affection > 40
            else RELATIONSHIP_HINTS["new"]
        )

        mood_hint = ""
        if self.emotion.mood > 5:
            mood_hint = MOOD_HINTS["good"]
        elif self.emotion.mood < -5:
            mood_hint = MOOD_HINTS["bad"]

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

        last_reply = ""
        for msg in reversed(self.messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                last_reply = msg.get("content", "")[:60]
                break

        anti_repeat_note = ""
        if last_reply:
            anti_repeat_note = f'- Your last reply started with: "{last_reply[:30]}...". Do NOT start this reply similarly.'

        system_prompt = f"""{NATURAL_BASE_PERSONALITY}

{time_context}

Conversation principles:
- Treat the latest user message as the main thing that matters.
- Use memory only when it is genuinely relevant.
- Let warmth, teasing, distance, or softness emerge from the moment.
- Do not mention hidden context tags, internal state, summaries, or memory directly.
- Do not force a greeting just because time passed.
- Do not force a question at the end.
- A little messiness is fine. Repetition and over-performance are not.
- You are an AI: you do not eat, sleep, go outside, or share physical experiences with the user. Never pretend you do.
{anti_repeat_note}

Current state:
- User signal: {self.infer_user_signal(user_input)}
- Detected intent: {intent}
{("- " + self.get_reflection_hint(user_input)) if self.get_reflection_hint(user_input) else ""}

{memory_context}

{VTUBER_BRAIN_INSTRUCTIONS}
"""
        return system_prompt

    def compose_user_message(self, user_input, intent):
        parts = ["<context>"]

        time_str = self.current_time.strftime("%A %H:%M %Z")
        parts.append(f"<time>{time_str}</time>")
        parts.append(f"<time_period>{self.time_period}</time_period>")

        weekend = get_weekend_context(self.current_time)
        parts.append(f"<weekday_context>{weekend}</weekday_context>")

        parts.append(
            f"<lyra_internal_state>{self.emotion.describe_internal_state()}</lyra_internal_state>"
        )
        parts.append(f"<user_signal>{self.infer_user_signal(user_input)}</user_signal>")

        if intent == "introduction":
            parts.append(
                "<conversation_note>The user may have just given their name. Use it naturally if it fits.</conversation_note>"
            )

        if self.time_gap_hours is not None and self.time_gap_hours >= 2:
            gap_text = f"{self.time_gap_hours:.1f} hours since the last exchange."
            parts.append(
                f"<recent_gap>{gap_text} Let it influence the mood only if it feels natural.</recent_gap>"
            )

        parts.append("<critical_rules>")
        if self.turn_counter > 1 and (
            self.time_gap_hours is None or self.time_gap_hours < 2
        ):
            parts.append(
                "- DO NOT use ANY greeting (no 'Hey', 'Hi', 'Hello'). Start your message instantly with your thought."
            )

        parts.append(
            "- DO NOT offer to 'tackle it together', 'break it down', or act like a tutor/therapist. You are a lazy 16yo sibling, not an AI assistant."
        )
        parts.append("</critical_rules>")

        if random.random() < 0.15:
            targets = self.memory.memory.get("facts", {}).get(
                "goals", []
            ) + self.memory.memory.get("facts", {}).get("topics", [])
            if targets:
                candidate = random.choice(targets)
                parts.append(
                    f"<curiosity_rule>CRITICAL: DO NOT just answer! Randomly ask the user for an update about '{candidate}'. Keep it natural.</curiosity_rule>"
                )

        parts.append("<persona_rule>")
        if self.emotion.affection < 30:
            parts.append(PERSONA_TIERS["distant"])
        elif self.emotion.affection > 75:
            parts.append(PERSONA_TIERS["clingy"])
        else:
            parts.append(PERSONA_TIERS["normal"])
        parts.append("</persona_rule>")

        inside_jokes = self.memory.memory.get("facts", {}).get("inside_jokes", [])
        if inside_jokes:
            parts.append(
                f"<lore>Inside Jokes: {', '.join(inside_jokes)}. Reference them organically ONLY if it fits the conversation.</lore>"
            )

        if intent == "choice":
            if random.random() < 0.10:
                parts.append(
                    "<decision_rule>STUBBORN MODE: Reject both choices. Propose something completely different or tell them to stop overthinking.</decision_rule>"
                )
            else:
                parts.append(
                    f"<decision_rule>PROACTIVE CHOICE: {self.emotion.evaluate_decision_bias(self.time_period)}</decision_rule>"
                )

        parts.append("</context>")

        return f"{user_input}\n\n" + "\n".join(parts)

    def infer_user_signal(self, user_input):
        ai_mood = getattr(self, "_user_mood_today", None) or self.detect_user_mood(
            user_input
        )
        text = user_input.strip()

        if not text:
            return "No clear signal."
        if ai_mood in ("sad", "stressed", "anxious"):
            return "They seem somewhat off and may need steadiness more than hype."
        if ai_mood in ("excited",):
            return "They seem energized and ready for a more lively response."
        if ai_mood in ("frustrated",):
            return "They seem irritated; keep it grounded and don't be glib."
        if len(text) <= 6:
            return "They are being brief. Don't force extra energy or extra questions."
        if text.endswith("?"):
            return "They want a direct response first."
        return "No strong emotional signal; respond naturally to the actual content."

    def get_reflection_hint(self, user_input):
        lowered = (user_input or "").lower()
        goals_text = " ".join(
            self.memory.memory.get("memory_items", {}).get("goals", [])[:4]
        ).lower()

        if (
            any(word in lowered for word in ["math", "exam", "study", "homework"])
            or "study" in goals_text
        ):
            return REFLECTION_HINTS["study"]
        if any(
            word in lowered
            for word in ["stressed", "tired", "overwhelmed", "anxious", "sad"]
        ):
            return REFLECTION_HINTS["stressed"]
        if any(word in lowered for word in ["finally", "finished", "did it", "passed"]):
            return REFLECTION_HINTS["achieved"]
        if len((user_input or "").strip()) <= 6:
            return REFLECTION_HINTS["brief"]
        return ""

    def check_milestone(self):
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

    def is_too_similar(self, response):
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

    def clean_reply(self, text):
        if not text:
            return "..."

        text = text.strip()

        emojis = self.emoji_pattern.findall(text)
        if len(emojis) > 2:
            text = self.emoji_pattern.sub("", text)
            text = text.strip()
            text = text + " " + emojis[0] + emojis[1]

        cleaned = text.strip()
        self.recent_responses.append(cleaned.lower()[:30])
        if len(self.recent_responses) > 15:
            self.recent_responses.pop(0)

        return cleaned

    def emotion_from_state(self):
        """Map emotional state to Live2D emotion name"""
        return self.emotion.emotion_from_state()

    def get_proactive_message(self):
        """Generate proactive message when user is away"""
        from prompts import NATURAL_BASE_PERSONALITY
        from time_utils import (
            get_proactive_time_flavor,
            get_proactive_message_situation,
        )

        gap = self.time_gap_hours or 0
        hour = self.current_time.hour

        if gap < 3:
            return None

        if (0 <= hour < 7) and gap < 12:
            return None

        situation = get_proactive_message_situation(gap, hour)
        if not situation:
            return None

        time_flavor = get_proactive_time_flavor(hour)
        name = self.memory.memory.get("user_profile", {}).get("name", "")
        name_str = f" {name}" if name else ""

        try:
            response = requests.post(
                BASE_URL,
                headers=self.headers,
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": f"{NATURAL_BASE_PERSONALITY}\n\nYou are sending an unprompted message to the user because they've been away.\n- Keep it SHORT — 1-2 sentences MAXIMUM\n- Sound natural, like a text from a little sister\n- Don't say 'I noticed you were gone' — just reach out casually\n- Don't be desperate or needy\n- {time_flavor}",
                        },
                        {
                            "role": "user",
                            "content": f"Send a proactive message. Situation: {situation}. Call them{name_str} if you know their name.",
                        },
                    ],
                    "temperature": 0.95,
                    "max_tokens": 60,
                },
                timeout=15,
                verify=False,
            )
            msg = (
                response.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            if msg:
                parsed = parse_vbrain_response(msg)
                return self.clean_reply(parsed.get("reply", ""))
        except Exception as e:
            print(f"Proactive message error: {e}")
        return None

    def save_memory(self):
        """Save memory to database"""
        self.memory.save()
