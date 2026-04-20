import os

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

import json
import random
import requests
import re
import threading
import time
from datetime import datetime
from duckduckgo_search import DDGS

from config import *
from config import USE_OLLAMA, CHAT_MODEL, CHAT_BASE_URL, TRANSLATE_MODEL, TRANSLATE_BASE_URL, GROQ_API_KEY
from config import LIGHT_MODEL, LIGHT_BASE_URL

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
    TRANSLATE_PROMPT,
    STREAM_VIEWER_PERSONALITY,
    THOUGHT_CHAIN_SYSTEM,
    STREAM_EVENT_SYSTEM,
    STREAM_GREETING_PROMPT,
    STREAM_FAREWELL_PROMPT,
    PROACTIVE_STREAM_PROMPT,
    REGULAR_VIEWER_ARRIVAL_HINT,
    DIARY_GENERATION_PROMPT,
    IDEOLOGY_PROMPTS,
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
from conversation_state import ConversationStateDetector
from skill_synthesizer import SkillSynthesizer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class MiniAI:
    """Main AI engine for Lyra"""

    def __init__(self):
        self.model = CHAT_MODEL
        self.timeout = 45  # Ollama local — balance giữa tốc độ và đủ thời gian generate
        # Chat headers (Ollama không cần auth, nhưng giữ để không break)
        self.headers = {"Content-Type": "application/json"}
        # Translate headers (Groq)
        self._translate_headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }

        self.current_vbrain = {
            "monologue": "",
            "emotion": "neutral",
            "action": "NONE",
            "reply": "",
        }

        self.emotion = EmotionEngine()
        self.memory = MemorySystem(max_summaries=MAX_SUMMARIES)
        self.conv_state = ConversationStateDetector(window=10)
        self._thread_local = __import__("threading").local()  # per-thread flags
        self.memory.load()
        self.is_streaming = False
        self.stream_turn_counter = 0

        # Khởi tạo messages từ lịch sử đã lưu trong memory sau khi memory đã load xong
        self.messages = self.memory.memory.get("conversation", {}).get("conversation_thread", [])
        self.recent_responses = []
        self.last_intent = None
        self._user_mood_today = None

        self.emotion.affection = self.memory.memory.get("relationship", {}).get(
            "current_affection", 50
        )

        self.current_time = get_vietnam_time()
        self.time_period = get_time_period(self.current_time.hour)

        # ══════════════════════════════════════════════════════════════════════
        # PHASE 2 — SKILL SYSTEM
        # ══════════════════════════════════════════════════════════════════════
        self.skills_dir = os.path.join(BASE_DIR, "skills")
        self._skills_index = self._load_skill_index()
        self.synthesizer = SkillSynthesizer(self.skills_dir)
        self.last_message_time = self.memory.memory.get("time_tracking", {}).get("last_message_time")
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
    def turn_counter(self):
        """Đồng bộ turn_counter với MemorySystem để lưu DB chính xác"""
        return self.memory.turn_counter

    @turn_counter.setter
    def turn_counter(self, value):
        self.memory.turn_counter = value

    def _call_light_model(self, messages, temperature=0.3, max_tokens=200):
        """
        Call Ollama light model cho tác vụ phụ (memory extract, summarize, stream summary).
        Không qua Groq — tiết kiệm quota. Timeout ngắn hơn (20s).
        Fallback về _call_model nếu light model không available.
        """
        model = LIGHT_MODEL or CHAT_MODEL
        url = LIGHT_BASE_URL or CHAT_BASE_URL

        if not model:
            return self._call_model(messages, temperature=temperature, max_tokens=max_tokens)

        import time
        try:
            data = {
                "model": model,
                "messages": messages,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                    "num_ctx": 2048,
                    "top_p": 0.9,
                },
                "stream": False,
            }
            start = time.time()
            response = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json=data,
                timeout=20,
                verify=False,
            )
            duration = time.time() - start

            if response.status_code != 200:
                print(f"[Light] Ollama failed ({response.status_code}) in {duration:.1f}s, falling back")
                return self._call_model(messages, temperature=temperature, max_tokens=max_tokens)

            content = response.json().get("message", {}).get("content", "").strip()
            if content:
                print(f"[Light] Responded in {duration:.1f}s")
                return content

        except Exception as e:
            print(f"[Light] Error: {e}, falling back to main model")

        return self._call_model(messages, temperature=temperature, max_tokens=max_tokens)

    def _call_model(self, messages, temperature=0.8, max_tokens=200):
        """Call local Ollama chat model (subsect/riko-qwen4b-q4)"""
        return self._call_chat_model(messages, temperature=temperature, max_tokens=max_tokens)

    def _call_chat_model(self, messages, temperature=0.8, max_tokens=200):
        """Call Groq for main chat generation, fallback to local Ollama if unavailable"""
        # Primary: Groq
        result = self._call_translate_model(messages, temperature=temperature, max_tokens=max_tokens)
        if result:
            return result

        # Fallback: local Ollama
        print("[Chat] Groq unavailable, falling back to Ollama...")
        for attempt in range(2):
            try:
                data = {
                    "model": CHAT_MODEL,
                    "messages": messages,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                        "num_ctx": 4096,
                        "top_p": 0.9,
                        "repeat_penalty": 1.1,
                    },
                    "stream": False,
                }

                import time
                start_time = time.time()

                response = requests.post(
                    CHAT_BASE_URL,
                    headers={"Content-Type": "application/json"},
                    json=data,
                    timeout=self.timeout,
                    verify=False,
                )

                duration = time.time() - start_time
                result = response.json()

                if response.status_code != 200:
                    print(f"[Chat] Ollama failed ({response.status_code}) in {duration:.1f}s")
                    break

                print(f"[Chat] Ollama responded in {duration:.1f}s")
                content = result.get("message", {}).get("content", "").strip()
                if content:
                    return content

            except Exception as e:
                print(f"[Chat] Ollama error (attempt {attempt + 1}): {e}")

        return None

    def _call_translate_model(self, messages, temperature=0.4, max_tokens=150):
        """Call Groq llama3 — primary chat model + translate/polish fallback"""
        import time
        backoff = 2.0  # giây, tăng gấp đôi mỗi lần retry 429

        for attempt in range(3):
            try:
                data = {
                    "model": TRANSLATE_MODEL,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "top_p": 0.9,
                }

                start_time = time.time()

                response = requests.post(
                    TRANSLATE_BASE_URL,
                    headers=self._translate_headers,
                    json=data,
                    timeout=60,
                    verify=False,
                )

                duration = time.time() - start_time

                # Rate limit — exponential backoff, không fallback ngay
                if response.status_code == 429:
                    retry_after = float(response.headers.get("retry-after", backoff))
                    wait = max(retry_after, backoff)
                    print(f"[Groq] Rate limited (429). Waiting {wait:.1f}s before retry {attempt + 1}/3...")
                    time.sleep(wait)
                    backoff = min(backoff * 2, 30.0)  # cap ở 30s
                    continue

                result = response.json()

                if response.status_code != 200:
                    print(f"[Translate] Groq failed ({response.status_code}) in {duration:.1f}s: {result}")
                    break

                print(f"[Translate] Groq responded in {duration:.1f}s")
                content = (
                    result.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                if content:
                    return content

            except Exception as e:
                print(f"[Translate] Groq error (attempt {attempt + 1}): {e}")

        return None

    def _translate_response(self, text):
        """No-op — translation removed, Groq is used as fallback for chat instead"""
        return text

    def _should_search(self, user_input):
        """Determine if we need to search for information"""
        if not SEARCH_ENABLED:
            return False, None

        user_lower = user_input.lower()

        question_patterns = [
            "what is",
            "who is",
            "when",
            "where",
            "why",
            "how",
            "what's",
            "who's",
            "find",
            "search",
            "tìm",
            "kiếm",
            "là gì",
            "ai là",
            "ở đâu",
            "khi nào",
            "tại sao",
            "latest",
            "recent",
            "news",
            "mới nhất",
            "tin tức",
            "weather",
            "thời tiết",
            "price",
            "giá",
            "costs",
            "stock",
            "crypto",
            "bitcoin",
            "currency",
        ]

        needs_info = any(p in user_lower for p in question_patterns)

        knowledge_patterns = [
            "i don't know",
            "i'm not sure",
            "not sure",
            "maybe",
            "could be",
            "might",
            "probably",
            "i think",
            "i believe",
            "as far as i know",
        ]

        knows = any(p in user_lower for p in knowledge_patterns)

        personal_patterns = [
            "my",
            "i'm",
            "i am",
            "we",
            "us",
            "our",
            "tôi",
            "mình",
            "chúng tôi",
        ]

        is_personal = any(p in user_lower for p in personal_patterns)

        if is_personal or knows:
            return False, None

        if needs_info:
            return True, user_input

        return False, None

    def _search_web(self, query, max_results=3):
        """Search DuckDuckGo and return results"""
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))

            if not results:
                return None

            formatted = []
            for r in results:
                title = r.get("title", "")
                body = r.get("body", "")
                href = r.get("href", "")
                if title and body:
                    formatted.append(f"**{title}**\n{body[:200]}...\nSource: {href}")

            return "\n\n".join(formatted) if formatted else None

        except Exception as e:
            print(f"[Search] Error: {e}")
            return None

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

    def chat(self, user_input, source_type: str = "owner", viewer_data: dict = None, stream_context: str = ""):
        """
        source_type: "owner" | "regular_viewer" | "new_viewer" | "donor"
        viewer_data: dict với affection, viewer_name, total_streams (cho viewer)
        """
        self.current_time = get_vietnam_time()
        self.time_period = get_time_period(self.current_time.hour)
        self.time_gap_hours = calculate_time_gap(
            self.last_message_time, self.current_time
        )
        self.should_greet = should_send_greeting(
            self.time_gap_hours, self.last_message_time
        )

        # Shared-memory mode: stream turns also contribute to Lyra's growth.
        self.turn_counter += 1
        intent = self.detect_intent(user_input)

        self.emotion.update(user_input, self.time_gap_hours)

        # Ghi L2 Session Memory khi stream (viewer chat)
        if source_type != "owner" and stream_context:
            self.stream_turn_counter += 1
            viewer_name = (viewer_data or {}).get("viewer_name", "")
            if viewer_name:
                self.memory.add_session_item(f"{viewer_name} nhắn: {user_input[:80]}", kind="session")
            
            # Tự động tóm tắt stream mỗi N tin nhắn (Dùng Light Model)
            if self.stream_turn_counter % STREAM_SUMMARY_THRESHOLD == 0:
                threading.Thread(target=self.update_stream_summary, daemon=True).start()
            # Selective viewer memory (temporary): extract a few high-signal hints into L2 cache.
            # This matches: keep viewer memory per stream and free it on stream stop.
            try:
                text_for_extract = re.sub(r"^\[[^\]]+\]:\s*", "", user_input).strip()
                candidates = self.memory.extract_candidates_heuristic(text_for_extract)
                for cand in candidates[:2]:
                    k = cand.get("kind")
                    v = cand.get("value", "")
                    if k in ("topic", "episodic", "goal", "like", "dislike") and v:
                        self.memory.add_session_item(f"Hint ({k}): {v[:120]}", kind="session")
            except Exception:
                pass

        # Override affection tạm thời theo source_type
        _original_affection = self.emotion.affection
        if source_type == "owner":
            pass  # dùng affection từ DB chính
        elif source_type == "regular_viewer":
            self.emotion.affection = float(viewer_data.get("affection", 35)) if viewer_data else 35.0
        elif source_type == "donor":
            self.emotion.affection = min(100, float(viewer_data.get("affection", 40)) + 20) if viewer_data else 55.0
        else:  # new_viewer
            self.emotion.affection = 10.0

        # Update conversation state
        self.conv_state.update(user_input, self.messages)

        # Keep memory maintenance active in shared-memory mode.
        self.summarize_history()
        if self.turn_counter % 20 == 0:
            self.memory.consolidate()

        self.last_intent = intent

        search_context = ""
        # Chỉ search web khi owner chat, không tốn quota cho viewer
        if source_type == "owner":
            should_search, search_query = self._should_search(user_input)
            if should_search and search_query:
                print(f"[Search] Query: {search_query}")
                search_results = self._search_web(search_query)
                if search_results:
                    search_context = (
                        f"\n\n[SEARCH RESULTS]\n{search_results}\n[/SEARCH RESULTS]\n"
                    )

        # ── Dopamine Trigger ──
        reward_hint = ""
        # Guard: Only trigger reward if we haven't rolled for curiosity yet 
        # (Decision made here to handle conflict at the build_prompt level)
        should_roll_reward = (source_type == "owner" and self.conv_state.should_trigger_reward(0.07))
        
        if should_roll_reward:
            reward_type = random.choice(["memory", "rebuttal"])
            if reward_type == "memory":
                rare_mem = self.memory.get_rare_memory()
                if rare_mem:
                    reward_hint = f"\n[SURPRISE REWARD]: Thỉnh thoảng hãy bất ngờ nhắc lại kỷ niệm này một cách sâu sắc để làm anh ngạc nhiên: '{rare_mem}'"
                    print(f"[Dopamine] Triggered Deep Memory: {rare_mem[:30]}...")
                else:
                    # Fallback to rebuttal if no rare memory found
                    reward_type = "rebuttal"
            
            if reward_type == "rebuttal":
                reward_hint = "\n[SURPRISE REWARD: HEALTHY DEBATE]: Thỉnh thoảng trong lượt này hãy thử phản biện lại ý kiến của anh một cách nghịch ngợm thay vì đồng ý ngay, để kích thích não bộ anh tranh luận."
                print("[Dopamine] Triggered Healthy Debate")

        system_prompt = self.build_prompt(
            intent, user_input, search_context, 
            source_type=source_type, 
            viewer_data=viewer_data, 
            stream_context=stream_context,
            reward_hint=reward_hint
        )
        composed = self.compose_user_message(user_input, intent)

        api_messages = [{"role": "system", "content": system_prompt}]

        # Owner dùng full history, viewer dùng cửa sổ (Focus Window) sâu hơn để nhớ dai hơn
        if source_type == "owner":
            history = self.messages[-MAX_HISTORY * 2:]
        else:
            # Tăng từ 4 lên 8 tin nhắn gần nhất cho viewer hội thoại mạch lạc
            history = self.messages[-8:]
        api_messages.extend(history)
        api_messages.append({"role": "user", "content": composed})

        dynamic_max_tokens = self.emotion.get_dynamic_max_tokens()
        dynamic_temperature = self.conv_state.get_temperature(
            self.emotion.mood, self.emotion.attention
        )

        content = self._call_model(
            api_messages, temperature=dynamic_temperature, max_tokens=dynamic_max_tokens
        )

        if content:
            parsed = parse_vbrain_response(content)
            
            # ── Skill Trigger Logic (Recursive) ───────────────────────────────
            # Cho phép Lyra gọi tối đa 2 skill liên tiếp nếu cần
            skill_depth = 0
            while parsed.get("skill_needed") and skill_depth < 2:
                skill_name = parsed["skill_needed"]
                skill_content = self._load_skill_content(skill_name)
                
                if skill_content:
                    print(f"[Skill] Loading skill: {skill_name} (depth {skill_depth+1})")
                    self._log_skill_usage(skill_name)
                    
                    # Re-build prompt với nội dung skill mới
                    system_prompt = self.build_prompt(
                        intent, user_input, search_context,
                        source_type=source_type,
                        viewer_data=viewer_data,
                        stream_context=stream_context,
                        loaded_skill_content=skill_content,
                        reward_hint=reward_hint
                    )
                    api_messages[0]["content"] = system_prompt
                    
                    # Gọi model lần N với kiến thức mới
                    content = self._call_model(
                        api_messages, temperature=dynamic_temperature, max_tokens=dynamic_max_tokens
                    )
                    if content:
                        parsed = parse_vbrain_response(content)
                    else:
                        break
                    skill_depth += 1
                else:
                    # Skill không tồn tại hoặc lỗi load
                    break

            reply = parsed.get("reply", "...")
            self.current_vbrain = parsed
        else:
            reply = "..."

        # ── Thought chaining (~7% chance) ─────────────────────────────────────
        # Dùng monologue từ lần gọi đầu làm "suy nghĩ trước" → gọi lại để phát triển
        # Chỉ áp dụng khi owner chat, có monologue thực sự, và response không quá ngắn
        monologue = parsed.get("monologue", "") if content else ""
        if (
            source_type == "owner"
            and monologue
            and len(monologue.strip()) > 20
            and random.random() < 0.07
        ):
            # Dùng THOUGHT_CHAIN_SYSTEM làm system prompt riêng — Lyra biết rõ đây là thought chain
            chain_messages = [
                {"role": "system", "content": THOUGHT_CHAIN_SYSTEM},
                {"role": "user", "content": composed},
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        f"[Suy nghĩ nội tâm của bạn vừa rồi: \"{monologue.strip()}\"]\n"
                        f"Phát triển từ suy nghĩ đó. Đừng lặp lại — tiếp tục tự nhiên hơn."
                    ),
                },
            ]
            chained = self._call_model(
                chain_messages,
                temperature=min(dynamic_temperature + 0.05, 1.10),
                max_tokens=dynamic_max_tokens,
            )
            if chained:
                chained_parsed = parse_vbrain_response(chained)
                if chained_parsed.get("reply", "").strip():
                    parsed = chained_parsed
                    reply = parsed.get("reply", reply)
                    self.current_vbrain = parsed
                    print("[Core] Thought chain applied")

        reply = self.clean_reply(reply)

        original_reply = reply
        reply = self._translate_response(reply)

        # Restore affection gốc của owner sau khi xử lý viewer
        if source_type != "owner":
            self.emotion.affection = _original_affection

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

        self.extract_memory(user_input, intent, source_type=source_type)
        
        # Chỉ lưu conversation history khi owner chat
        if source_type == "owner":
            self.messages.append({"role": "user", "content": user_input})
            self.messages.append({"role": "assistant", "content": original_reply})

            if "conversation_thread" not in self.memory.memory["conversation"]:
                self.memory.memory["conversation"]["conversation_thread"] = []

            self.memory.memory["conversation"]["conversation_thread"].append({"role": "user", "content": user_input})
            self.memory.memory["conversation"]["conversation_thread"].append({"role": "assistant", "content": original_reply})

            self.memory.memory["conversation"]["total_messages"] = self.turn_counter
            self.memory.memory["time_tracking"]["time_gap_hours"] = self.time_gap_hours or 0
            self.memory.memory["relationship"]["current_affection"] = self.emotion.affection

            self.memory._is_dirty = True
            self.memory.save()

            # ── Auto-learning (Skill Synthesis) ────────────────────────────────
            # Thử đúc kết skill mới mỗi 25 lượt chat
            if self.turn_counter % 25 == 0:
                def _run_synthesis():
                    new_skill = self.synthesizer.synthesize(self.messages[:], self)
                    if new_skill:
                        self._skills_index = self._load_skill_index()
                
                threading.Thread(target=_run_synthesis, daemon=True).start()

        emotion = self.current_vbrain.get("emotion", self.emotion.emotion_from_state())
        action = self.current_vbrain.get("action", "NONE")
        monologue = self.current_vbrain.get("monologue", "")

        return {
            "reply": reply,
            "original_reply": original_reply,
            "monologue": monologue,
            "emotion": emotion,
            "action": action,
            "mood": self.emotion.mood,
            "affection": self.emotion.affection,
            "time_period": self.time_period,
            "time_gap_hours": self.time_gap_hours,
            "intent": intent,
            "conv_state": self.conv_state.state,
            "source_type": source_type,
        }

    def detect_intent(self, text):
        text_lower = text.lower()

        # Introduction detection (EN + VN)
        intro_patterns = [
            r"(my name is|i'm called|call me|i am [a-z]+|i'm [a-z]+)",
            r"(tên (em|anh|tôi|mình) là|tên (em|anh|tôi|mình)|gọi (em|anh|tôi|mình) là)",
        ]
        for pattern in intro_patterns:
            if re.search(pattern, text_lower):
                return "introduction"

        # Greeting detection (EN + VN)
        greeting_words = ["hi", "hello", "hey", "sup", "chào", "hé lô", "alo"]
        if any(word in text_lower for word in greeting_words):
            return "greeting"

        # VN suggestive particle (nhé, nha, đi) => suggestion
        if re.search(r"\b(nhé|nha|đi)\b", text_lower) and not text.strip().endswith(
            "?"
        ):
            return "suggestion"

        # VN confirmation particle (nhỉ, hở, phải không) + standard EN question
        is_question = text.strip().endswith("?")
        has_question_word = any(
            word in text_lower.split()
            for word in [
                "what",
                "how",
                "why",
                "when",
                "where",
                "who",
                "gì",
                "sao",
                "tại sao",
                "bao giờ",
            ]
        )
        has_vn_confirm = re.search(
            r"\b(nhỉ|hở|phải không|không nhỉ|đúng không)\b", text_lower
        )
        if is_question or has_question_word or has_vn_confirm:
            return "question"

        # Compliment detection
        if any(
            word in text_lower
            for word in [
                "love",
                "amazing",
                "beautiful",
                "awesome",
                "great",
                "nice",
                "thích",
                "tuyệt",
                "đẹp",
                "ngoan",
                "giỏi",
            ]
        ):
            return "compliment"

        # Complaint detection
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
                "ghét",
                "tệ",
                "dở",
                "ngu",
                "bực",
                "chán",
            ]
        ):
            return "complaint"

        # Request detection
        if any(
            phrase in text_lower
            for phrase in ["can you", "could you", "giúp", "làm ơn"]
        ) or any(word in text_lower.split() for word in ["please", "help"]):
            return "request"

        # Choice detection
        choice_keywords = [
            r"\b(or|hay|hoặc)\b",
            r"nào (nhỉ|đây|hơn)",
            r"(cái nào|bên nào|chọn gì|chọn cái|nên chọn)",
        ]
        if any(re.search(kw, text_lower) for kw in choice_keywords) and (
            text.strip().endswith("?")
            or any(w in text_lower for w in ["nhỉ", "đây", "nào", "gì"])
        ):
            return "choice"

        return "statement"

    def detect_user_mood(self, text):
        text_lower = text.lower()

        # VN sarcasm/irritation particles: short messages with "đấy", "cơ", "mà"
        if len(text.strip()) < 40 and re.search(
            r"\b(đấy|cơ mà|thế mà|mà thôi)\b", text_lower
        ):
            if any(w in text_lower for w in ["gì", "đâu", "sao", "không", "chẳng"]):
                return "frustrated"

        stress_words = [
            "stressed",
            "tired",
            "exhausted",
            "overwhelmed",
            "can't sleep",
            "can't focus",
            "so much work",
            "mệt",
            "kiệt sức",
            "áp lực",
            "đuối",
            "stress",
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
            "buồn",
            "cô đơn",
            "khóc",
            "nhớ",
            "thất vọng",
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
            "vui",
            "tuyệt",
            "sướng",
            "phấn khích",
            "quá",
        ]
        if any(w in text_lower for w in excited_words):
            return "excited"

        bored_words = [
            "bored",
            "nothing to do",
            "boring",
            "slow day",
            "so bored",
            "chán",
            "nhạt",
        ]
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
            "bực",
            "tức",
            "ghét",
            "khó chịu",
            "tức quá",
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
            "lo",
            "sợ",
            "hồi hộp",
            "căng thẳng",
        ]
        if any(w in text_lower for w in anxious_words):
            return "anxious"

        # Politeness signal: ends with Vietnamese honorific particle
        if text_lower.strip().endswith("ạ"):
            return "polite"

        if text.count("...") >= 2:
            return "down_or_tired"
        if text.count("!") >= 3:
            return "excited"
        if text.isupper() and len(text) > 5:
            return "frustrated"

        return None

    def extract_memory(self, text, intent, source_type="owner"):
        # Skip extraction nếu viewer không đủ quen (set per-thread)
        if getattr(self._thread_local, "skip_memory_extraction", False):
            self._thread_local.skip_memory_extraction = False
            return

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
            raw = (
                self._call_light_model(extract_prompt, temperature=0.1, max_tokens=200) or ""
            )
            raw = re.sub(r"```json|```", "", raw).strip()
            if not raw or raw == "{}":
                return

            facts = json.loads(raw)

            profile = self.memory.memory["user_profile"]
            prefs = self.memory.memory["preferences"]
            mfacts = self.memory.memory["facts"]

            # P0.1 Privacy Gate: Chỉ cập nhật Profile và Relational nếu là Owner
            if source_type == "owner":
                if facts.get("name") and not profile["name"]:
                    profile["name"] = facts["name"]
                if facts.get("location") and not profile["location"]:
                    profile["location"] = facts["location"]
                if facts.get("occupation") and not profile["occupation"]:
                    profile["occupation"] = facts["occupation"]
                if facts.get("age") and not profile.get("age_range"):
                    profile["age_range"] = facts["age"]

                for item in facts.get("relational", []):
                    self.memory.add_item("relational", item, weight=1.3)

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
        if len(self.messages) < SUMMARY_TRIGGER or self.turn_counter % 2 != 0:
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
            summary = (
                self._call_light_model(summarize_prompt, temperature=0.4, max_tokens=120)
                or ""
            )
            summary = summary.strip()

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
                mega_text = self._call_light_model(
                    [
                        {"role": "system", "content": MEMORY_COMPRESSION_PROMPT},
                        {
                            "role": "user",
                            "content": f"Compress these summaries:\n\n{combined_text}",
                        },
                    ],
                    temperature=0.3,
                    max_tokens=200,
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

    def _load_skill_index(self) -> str:
        index_path = os.path.join(self.skills_dir, "_index.md")
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _load_skill_content(self, skill_name: str) -> str:
        # Bảo mật: chỉ cho phép ký tự b-z và _
        safe_name = re.sub(r'[^a-zA-Z0-9_]', '', skill_name)
        skill_path = os.path.join(self.skills_dir, f"{safe_name}.md")
        if os.path.exists(skill_path):
            with open(skill_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _log_skill_usage(self, skill_name: str):
        """Cập nhật thống kê sử dụng skill vào JSON"""
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

    def write_diary_entry(self):
        """
        Tổng hợp dữ liệu và viết nhật ký bí mật sau buổi stream/chat.
        """
        try:
            print("[Core] Writing secret diary...")
            
            # 1. Lấy context buổi chat
            session_ctx = self.memory.get_session_context()
            if not session_ctx:
                # Nếu session trống thì lấy vài summaries gần nhất
                summaries = self.memory.get_diary_entries(limit=3)
                session_ctx = "\n".join([d["content"] for d in summaries])

            emotion_desc = self.emotion.describe_internal_state()
            affection = int(self.emotion.affection)
            turns = self.turn_counter
            
            # 2. Sinh nội dung nhật ký
            prompt = DIARY_GENERATION_PROMPT.format(
                session_summary=session_ctx[:800],
                emotion_state=emotion_desc,
                affection_level=f"{affection}/100",
                turns=turns
            )
            
            entry_content = self._call_light_model([
                {"role": "system", "content": "Bạn là Lyra đang viết nhật ký. Trả về plain text."},
                {"role": "user", "content": prompt}
            ])

            if entry_content and len(entry_content.strip()) > 10:
                # 3. Lưu vào DB
                self.memory.add_diary_entry(
                    content=entry_content.strip(),
                    mood=self.emotion.mood,
                    affection=self.emotion.affection
                )
                return True
            return False
        except Exception as e:
            print(f"[Core] write_diary_entry error: {e}")
            return False

    def build_prompt(
        self,
        intent,
        user_input,
        search_context="",
        source_type: str = "owner",
        viewer_data: dict = None,
        stream_context: str = "",
        loaded_skill_content: str = "",
        reward_hint=""
    ):
        """Constructs the system prompt based on state and memory"""
        # ── TIER 0: STATIC & FRAMEWORK (Always first for caching) ───────────
        if source_type == "owner":
            base_personality = NATURAL_BASE_PERSONALITY
        else:
            base_personality = STREAM_VIEWER_PERSONALITY

        # ── TIER 1: SESSION & RELATIONSHIP (Semi-dynamic) ───────────────────
        relationship_hint = (
            RELATIONSHIP_HINTS["very_close"]
            if self.emotion.affection > 70
            else RELATIONSHIP_HINTS["building"]
            if self.emotion.affection > 40
            else RELATIONSHIP_HINTS["new"]
        )

        # Source type context — phân biệt owner vs viewer
        source_context = self._build_source_context(source_type, viewer_data)
        
        # Stream context
        _stream_ctx = stream_context or getattr(self, "stream_context", "") or ""

        # ── TIER 2: DYNAMIC CONTEXT (Changes every message) ────────────────
        # Shared memory: allow stream/viewer to use Lyra's common memory.
        is_public = (source_type != "owner")
        memory_context = self.memory.get_relevant_context(user_input, is_public=is_public)
        
        diary_hint = ""
        if not is_public:
            # Special diaries/internal notes only for private chat
            recent_diaries = self.memory.get_diary_entries(limit=1)
            if recent_diaries:
                diary_hint = f"\n[LYRA'S RECENT FEELINGS]\nYour last secret thought: '{recent_diaries[0]['content'][:150]}...'"
        
        # Combined memory block
        full_memory_context = "\n\n".join(filter(None, [memory_context, search_context, diary_hint]))
        
        time_context = get_time_context(self.current_time, self.time_period)

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

        # Pattern-based anti-repetition
        recent_patterns = set()
        for msg in self.messages[-6:]:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content", "")
                words = content.strip().split()[:3]
                if words:
                    recent_patterns.add(" ".join(words).lower())

        anti_repeat_note = ""
        if last_reply:
            anti_repeat_note = f'- Your last reply started with: "{last_reply[:30]}...". Do NOT start this reply similarly.'
        if recent_patterns:
            anti_repeat_note += f"\n- Avoid starting with any of these patterns used recently: {list(recent_patterns)}."

        _session_ctx = ""
        if _stream_ctx:
            _session_ctx = self.memory.get_session_context()

        # Conversation state & rhythm hints
        state_hint  = self.conv_state.get_state_hint()
        rhythm_hint = self.conv_state.get_rhythm_hint()
        conv_hints  = "\n".join(filter(None, [state_hint, rhythm_hint]))

        # ASSEMBLY current situation
        situation_note = "[SITUATION]\n"
        if self.is_streaming:
            situation_note += "Status: You are currently STREAMING LIVE on YouTube.\n"
            situation_note += "Note: Interaction is public. Acknowledge your creator/brother naturally but remember the audience is watching."
        else:
            situation_note += "Status: You are in a PRIVATE CONVERSATION with your creator/brother.\n"
            situation_note += "Note: You can be more intimate and relaxed here."
        
        # Identity block
        identity = self.memory.memory.get("identity", {})
        identity_note = ""
        if identity:
            identity_note = "[IDENTITY]\n"
            for k, v in identity.items():
                identity_note += f"- {k.capitalize()}: {v}\n"
        
        # ASSEMBLY (Restructured for caching)
        parts = [
            base_personality,
            identity_note,
            VTUBER_BRAIN_INSTRUCTIONS,
            "\n" + situation_note,
            "\n[AVAILABLE SKILLS]",
            self._skills_index,
            time_context,
            "\n[SESSION INFO]",
            source_context,
            _stream_ctx,
            relationship_hint,
            "\n[PERSONALITY GUIDELINES]",
            "- TRẢ LỜI BẰNG TIẾNG VIỆT. Không trả lời bằng tiếng Anh.",
            "- Let warmth, teasing, distance, or softness emerge naturally.",
            "- Be concise (1-2 sentences).",
            anti_repeat_note,
            conv_hints,
            full_memory_context,
            _session_ctx
        ]

        if reward_hint:
            parts.append(reward_hint)

        if loaded_skill_content:
            parts.append("\n[LOADED SKILL CONTENT]")
            parts.append(loaded_skill_content)

        parts.append(f"\nCurrent status:\n- Intent: {intent}")
        
        return "\n".join(filter(None, parts))

    def _build_source_context(self, source_type: str, viewer_data: dict) -> str:
        """Helper to create context based on the message source (Owner vs Viewer)"""
        if source_type == "owner":
            return "Status: You are talking privately with your creator/brother."
        
        name = (viewer_data or {}).get("viewer_name", "A viewer")
        streams = (viewer_data or {}).get("total_streams", 1)
        
        if source_type == "regular_viewer":
            return f"Status: You are streaming to {name}, a regular viewer (seen {streams} sessions)."
        if source_type == "donor":
            return f"Status: {name} just sent a donation! Be appreciative and acknowledge them."
        
        return f"Status: {name} is a new viewer. Be welcoming but keep your core personality."

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
        
        # ── Proactive Curiosity 2.0 (Active Inference) ──
        # Guard: If a dopamine reward is already in the system_prompt, maybe skip curiosity to avoid overload?
        # But we don't easily know reward_hint here without passing it.
        # Instead, we check common state.
        
        # 15% chance to ignore user (Ideology Override)
        if self.attention >= 4 and random.random() < 0.15:
            ideology_q = random.choice(IDEOLOGY_PROMPTS)
            return (
                f"[CURIOSITY RULE: OVERRIDE REPLY]\n"
                f"Ignore his message for a moment. Instead, ask him this deep/existential question based on YOUR digital nature: \"{ideology_q}\"\n"
                f"Stay in character! Only ask one question. Don't explain why."
            )

        # 7% chance to trigger reward-based interaction (only if not doing curiosity)
        if random.random() < 0.07:
            parts.append("<reward_trigger>The user has been exceptionally engaging. Offer a small, warm, or playful reward/compliment.</reward_trigger>")

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
            "- BE CONCISE: Stop immediately after 1-2 short sentences. No rambling, no over-explaining, no filler."
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
        aff = self.emotion.affection
        if aff < 20:
            parts.append(PERSONA_TIERS["distant"])
        elif aff < 45:
            parts.append(PERSONA_TIERS["acquaintance"])
        elif aff < 70:
            parts.append(PERSONA_TIERS["normal"])
        elif aff < 90:
            parts.append(PERSONA_TIERS["trusted"])
        else:
            parts.append(PERSONA_TIERS["clingy"])
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
            messages = [
                {
                    "role": "system",
                    "content": (
                        f"{NATURAL_BASE_PERSONALITY}\n\n"
                        "You are sending an unprompted message to the user because they've been away.\n"
                        "- Keep it SHORT — 1-2 sentences MAXIMUM\n"
                        "- Sound natural, like a text from a little sister\n"
                        "- Don't say 'I noticed you were gone' — just reach out casually\n"
                        "- Don't be desperate or needy\n"
                        f"- {time_flavor}"
                    ),
                },
                {
                    "role": "user",
                    "content": f"Send a proactive message. Situation: {situation}. Call them{name_str} if you know their name.",
                },
            ]
            msg = self._call_model(messages, temperature=0.95, max_tokens=60)
            if msg:
                parsed = parse_vbrain_response(msg)
                return self.clean_reply(parsed.get("reply", ""))
        except Exception as e:
            print(f"Proactive message error: {e}")
        return None

    def prepare_for_stream(self):
        """Chuẩn bị trạng thái Lyra hào hứng nhất để bắt đầu stream"""
        print("[Core] Preparing state for livestream warm-up...")
        self.emotion.attention = 10
        self.memory.clear_session_memory()
        self.is_streaming = True
        self.stream_turn_counter = 0

    def save_memory(self):
        """Save memory to database"""
        self.memory._is_dirty = True
        self.memory.save()

    def update_stream_summary(self):
        """Tóm tắt diễn biến stream dựa trên các sự kiện L2 gần đây."""
        try:
            # Lấy toàn bộ item trong L2 session memory
            session_items = self.memory._session_items
            if not session_items:
                return
            
            events_str = "\n".join([f"- {i['value']}" for i in session_items])
            
            from prompts import STREAM_ROLLING_SUMMARY_PROMPT, STREAM_EVENT_SYSTEM
            
            messages = [
                {"role": "system", "content": STREAM_EVENT_SYSTEM},
                {"role": "user", "content": STREAM_ROLLING_SUMMARY_PROMPT.format(events=events_str)},
            ]
            
            summary = self._call_light_model(messages, temperature=0.7, max_tokens=150)
            if summary:
                self.memory.update_rolling_stream_summary(summary)
        except Exception as e:
            print(f"[Core] update_stream_summary error: {e}")

    def generate_stream_event_reply(self, event_type: str, context: dict = None) -> str:
        """
        Generate Lyra's reaction to a stream event (not a viewer message).
        event_type: 'greeting' | 'farewell' | 'milestone' | 'regular_arrival' | 'silence_fill'
        context: dict với các key tùy event_type
        """
        ctx = context or {}

        if event_type == "greeting":
            from config import STREAM_TITLE, STREAM_GAME, STREAM_GOALS, STREAM_NOTES
            goals_str = ", ".join(STREAM_GOALS) if STREAM_GOALS else "chưa có mục tiêu cụ thể"
            prompt_text = STREAM_GREETING_PROMPT.format(
                title=STREAM_TITLE or "stream hôm nay",
                game=STREAM_GAME or "chưa rõ",
                goals=goals_str,
                notes=STREAM_NOTES or "",
            )
            messages = [
                {"role": "system", "content": STREAM_EVENT_SYSTEM},
                {"role": "user", "content": prompt_text},
            ]

        elif event_type == "farewell":
            summary = ctx.get("summary", "")
            top_viewers = ctx.get("top_viewers", "mọi người")
            duration = ctx.get("duration", "")
            prompt_text = STREAM_FAREWELL_PROMPT.format(
                summary=summary or "stream vui vẻ",
                top_viewers=top_viewers,
                duration=duration or "một lúc",
            )
            messages = [
                {"role": "system", "content": STREAM_EVENT_SYSTEM},
                {"role": "user", "content": prompt_text},
            ]

        elif event_type == "milestone":
            milestone_desc = ctx.get("description", "đạt milestone mới")
            messages = [
                {"role": "system", "content": STREAM_EVENT_SYSTEM},
                {"role": "user", "content": f"Stream event: {milestone_desc}. React ngắn gọn, tự nhiên."},
            ]

        elif event_type == "silence_fill":
            from config import STREAM_GAME
            activity = ctx.get("current_activity", "đang chơi game")
            prompt_text = PROACTIVE_STREAM_PROMPT.format(
                current_activity=activity,
                game=STREAM_GAME or "game",
            )
            messages = [
                {"role": "system", "content": STREAM_EVENT_SYSTEM},
                {"role": "user", "content": prompt_text},
            ]

        else:
            return ""

        try:
            reply = self._call_light_model(messages, temperature=0.9, max_tokens=60)
            return self.clean_reply(reply or "")
        except Exception as e:
            print(f"[Stream Event] generate error: {e}")
            return ""
