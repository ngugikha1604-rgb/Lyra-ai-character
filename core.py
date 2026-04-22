import os

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

import concurrent.futures
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

    REWARD_HINTS,

    ILLOCUTION_HINTS,

    SELF_DISCLOSURE_TEMPLATES,

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

        self._last_disclosure_turn = 0  # Self-Disclosure Engine cooldown tracker


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

        # ThreadPoolExecutor for async I/O operations
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


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

        result = self._call_groq_model(messages, temperature=temperature, max_tokens=max_tokens)

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

    def _call_groq_model(self, messages, temperature=0.4, max_tokens=150):

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


        # ── Speech Act Classifier (Austin/Searle) ────────────────────────────

        # Chỉ classify cho owner — không tốn context cho viewer

        # illocution_type dùng để log; perlocution_hint inject vào build_prompt

        if source_type == "owner":

            _illocution_type, _perlocution_hint = self.classify_illocution(user_input, intent)

        else:

            _illocution_type, _perlocution_hint = "neutral", ""


        self.emotion.update(user_input, self.time_gap_hours, intent=intent)


        # ── Self-Disclosure Engine (Walther — SIP Theory) ────────────────────

        # Gọi SAU emotion.update() để dùng emotion state đã cập nhật

        # Chỉ owner chat; _illocution_type từ Speech Act Classifier làm input signal

        # Guard: không trigger khi reward_hint đã được set (tránh conflict directive)

        # NOTE: reward_hint chưa được tính ở đây — guard sẽ được apply sau reward block

        if source_type == "owner":

            _self_disclosure_hint = self._get_self_disclosure_hint(intent, _illocution_type)

        else:

            _self_disclosure_hint = ""


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

        _original_dominance = self.emotion.dominance  # Save dominance — restore sau viewer turn

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

        # ── Parallel: web search + memory ranking ────────────────────────────
        # Hai I/O calls này không phụ thuộc nhau → chạy song song tiết kiệm ~1-3s
        _needs_search = False
        _search_query = ""
        if source_type == "owner":
            _needs_search, _search_query = self._should_search(user_input)
            if _needs_search and _search_query:
                print(f"[Search] Query: {_search_query}")

        is_public = (source_type != "owner")

        # Submit cả 2 tasks vào thread pool cùng lúc
        _memory_future = self._executor.submit(
            self.memory.get_relevant_context, user_input, is_public
        )
        _search_future = None
        if _needs_search and _search_query:
            _search_future = self._executor.submit(self._search_web, _search_query)

        # Collect results (blocking chờ cả 2 xong — nhưng chạy song song)
        _precomputed_memory = _memory_future.result() or ""
        search_context = ""
        if _search_future is not None:
            raw_search = _search_future.result()
            if raw_search:
                search_context = f"\n\n[SEARCH RESULTS]\n{raw_search}\n[/SEARCH RESULTS]\n"


        # ── Variable Ratio Reinforcement (Skinner) ──────────────────────────

        # should_trigger_reward() trả về reward type string hoặc None.

        # Priority: reward > ideology > surprise — không bao giờ 2 mode cùng lúc.

        reward_hint = ""

        reward_type = None


        if source_type == "owner":

            reward_type = self.conv_state.should_trigger_reward(0.07)


        if reward_type == "deep_recall":

            rare_mem = self.memory.get_rare_memory()

            if rare_mem:

                template = random.choice(REWARD_HINTS["deep_recall"])

                reward_hint = template.format(memory=rare_mem)

                self.conv_state.confirm_reward_delivered()

                print(f"[Reward] deep_recall: {rare_mem[:40]}...")

            else:

                # Không có rare memory → degrade sang healthy_debate

                reward_type = "healthy_debate"


        # Dùng if (không elif) để deep_recall fallback vào đây được

        if reward_type == "healthy_debate":

            template = random.choice(REWARD_HINTS["healthy_debate"])

            reward_hint = template

            self.conv_state.confirm_reward_delivered()

            print("[Reward] healthy_debate")


        elif reward_type == "vulnerability":

            if self.emotion.irritability < 0.4:

                template = random.choice(REWARD_HINTS["vulnerability"])

                reward_hint = template

                self.conv_state.confirm_reward_delivered()

                print("[Reward] vulnerability")

            elif self.emotion.mood >= -2:

                # Lyra đang bực — fallback sang silent_approval nếu mood OK

                template = random.choice(REWARD_HINTS["silent_approval"])

                reward_hint = template

                self.conv_state.confirm_reward_delivered()

                print("[Reward] vulnerability→silent_approval (irritability high)")

            else:

                # Cả 2 điều kiện đều fail — skip, không consume cooldown

                reward_hint = ""

                print("[Reward] vulnerability skipped (irritability high + bad mood)")


        elif reward_type == "curiosity_spike":

            if self.emotion.attention >= 4:

                template = random.choice(REWARD_HINTS["curiosity_spike"])

                reward_hint = template

                self.conv_state.confirm_reward_delivered()

                print("[Reward] curiosity_spike")

            else:

                # Skip, không consume cooldown

                reward_hint = ""

                print("[Reward] curiosity_spike skipped (low attention)")


        elif reward_type == "silent_approval":

            if self.emotion.mood >= -2:

                template = random.choice(REWARD_HINTS["silent_approval"])

                reward_hint = template

                self.conv_state.confirm_reward_delivered()

                print("[Reward] silent_approval")

            else:

                # Skip, không consume cooldown

                reward_hint = ""

                print("[Reward] silent_approval skipped (bad mood)")


        # ── Guard: self-disclosure vs reward conflict ─────────────────────────

        # Nếu reward đã được deliver, clear self-disclosure để tránh 2 directive mâu thuẫn

        if reward_hint and _self_disclosure_hint:

            _self_disclosure_hint = ""

            print("[Self-Disclosure] Skipped due to reward conflict")


        # ── Active Inference Mode (Phần 4) ──────────────────────────────────

        # Quyết định tại 1 điểm duy nhất — tránh conflict giữa build_prompt và compose_user_message

        # Priority: reward > ideology > surprise (không bao giờ 2 mode cùng lúc)

        active_inference_mode = None  # None | "ideology" | "surprise"

        _ideology_idx = -1


        if source_type == "owner" and not reward_hint:

            # Thử ideology trước (có random roll bên trong should_trigger_ideology)

            # Guard attention >= 4: Lyra phải đủ tỉnh táo mới hỏi existential

            if self.attention >= 4:

                _ideology_idx = self.conv_state.should_trigger_ideology(

                    len(IDEOLOGY_PROMPTS), min_cooldown=5

                )

            if _ideology_idx >= 0:

                active_inference_mode = "ideology"

                # Cross-cooldown: reset surprise timer để tránh 2 lượt liên tiếp

                self.conv_state._last_surprise_turn = self.conv_state._turn

            elif self.conv_state.should_trigger_surprise(probability=0.05, min_cooldown=5):

                active_inference_mode = "surprise"


        # ── Guard: self-disclosure vs active_inference conflict ───────────────

        # Ideology override và surprise cũng là behavioral directives — không inject cùng lúc

        if active_inference_mode and _self_disclosure_hint:

            _self_disclosure_hint = ""

            print("[Self-Disclosure] Skipped due to active_inference conflict")


        system_prompt = self.build_prompt(
            intent, user_input, search_context,
            source_type=source_type,
            viewer_data=viewer_data,
            stream_context=stream_context,
            reward_hint=reward_hint,
            active_inference_mode=active_inference_mode,
            perlocution_hint=_perlocution_hint,
            self_disclosure_hint=_self_disclosure_hint,
            precomputed_memory_context=_precomputed_memory,
        )

        composed = self.compose_user_message(
            user_input, intent,

            reward_active=bool(reward_hint),

            ideology_idx=_ideology_idx,

        )


        api_messages = [{"role": "system", "content": system_prompt}]


        # Owner dùng full history, viewer dùng cửa sổ (Focus Window) sâu hơn để nhớ dai hơn

        if source_type == "owner":

            history = self.messages[-MAX_HISTORY * 2:]

        else:

            # Tăng từ 4 lên 8 tin nhắn gần nhất cho viewer hội thoại mạch lạc

            history = self.messages[-8:]

        api_messages.extend(history)

        api_messages.append({"role": "user", "content": composed})


        # ── Pace Sync: base tokens từ emotion, sau đó điều chỉnh theo user pace ──

        _base_tokens = self.emotion.get_dynamic_max_tokens()

        dynamic_max_tokens = self.conv_state.get_pace_max_tokens(_base_tokens)

        dynamic_temperature = self.conv_state.get_temperature(
            self.emotion.mood, self.emotion.attention, self.emotion.dominance

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
                        reward_hint=reward_hint,
                        perlocution_hint=_perlocution_hint,
                        self_disclosure_hint=_self_disclosure_hint,
                        precomputed_memory_context=_precomputed_memory,  # reuse, không fetch lại
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

            parsed = {}  # Bug fix: đảm bảo parsed luôn defined để tránh NameError

            # Reset vbrain state khi model fail — tránh trả về emotion/monologue cũ

            self.current_vbrain = {

                "monologue": "", "emotion": "neutral", "action": "NONE", "reply": "..."

            }


        # ── Thought chaining (~7% chance) ─────────────────────────────────────

        # Dùng monologue từ lần gọi đầu làm "suy nghĩ trước" → gọi lại để phát triển

        # Chỉ áp dụng khi owner chat, có monologue thực sự, và response không quá ngắn

        monologue = parsed.get("monologue", "")

        if (

            source_type == "owner"

            and monologue

            and len(monologue.strip()) > 20

            and content  # Guard: content phải còn valid (không None) để dùng làm assistant message

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

        original_reply = reply  # Bug fix: lưu reply sạch TRƯỚC khi inject filler

        reply = self._maybe_add_filler(reply, user_input, source_type)


        reply = self._translate_response(reply)


        # Restore affection và dominance gốc của owner sau khi xử lý viewer

        if source_type != "owner":

            self.emotion.affection = _original_affection

            self.emotion.dominance = _original_dominance


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


        # ── Fire-and-forget: extract_memory (không block reply) ─────────────
        # Chạy sau khi reply đã sẵn sàng — không cần kết quả để trả về user
        # Capture skip flag TRƯỚC khi spawn thread (thread_local không cross-thread)
        _should_skip_extract = getattr(self._thread_local, "skip_memory_extraction", False)
        if _should_skip_extract:
            self._thread_local.skip_memory_extraction = False  # reset ngay
        else:
            _extract_input = user_input
            _extract_intent = intent
            _extract_source = source_type
            threading.Thread(
                target=self.extract_memory,
                args=(_extract_input, _extract_intent, _extract_source),
                daemon=True,
            ).start()

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

            # Update last_message_time để time_gap_hours tính đúng trong session
            now_iso = self.current_time.isoformat()
            self.last_message_time = now_iso
            self.memory.memory["time_tracking"]["last_message_time"] = now_iso

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

            "dominance": round(self.emotion.dominance, 2),

            "irritability": round(self.emotion.irritability, 2),

            "vad": self.emotion.get_vad(),

            "time_period": self.time_period,

            "time_gap_hours": self.time_gap_hours,

            "intent": intent,

            "illocution": _illocution_type,

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


    def classify_illocution(self, text: str, intent: str) -> tuple:
        """

        Speech Act Classifier — Layer 2: Illocution + Perlocution (Austin/Searle).


        Phân tích *mục đích thực sự* đằng sau câu nói (Illocution) và trả về

        behavioral directive (Perlocution hint) để inject vào system prompt.


        Không thay thế detect_intent() — bổ sung thêm một lớp hiểu ngữ nghĩa.

        Heuristic-based, không LLM call, không state mới.


        Args:

            text:   raw user input

            intent: kết quả từ detect_intent() — dùng làm signal phụ


        Returns:

            (illocution_type: str, perlocution_hint: str)

            illocution_type: "expressive" | "directive" | "commissive" |

                             "assertive" | "declarative" | "neutral"

            perlocution_hint: string để inject vào system prompt, hoặc ""
        """

        text_lower = text.lower().strip()

        text_len = len(text_lower)


        # ── Expressive: chia sẻ cảm xúc, than thở, không cần giải pháp ──────

        # Signals: từ cảm xúc tiêu cực/tích cực + không có dấu hỏi + không request

        expressive_signals = [

            # Tiêu cực

            "mệt", "mệt quá", "mệt rồi", "buồn", "chán", "stress", "áp lực",

            "tệ quá", "tệ thật", "khó chịu", "bực", "tức", "đau", "khổ",

            "cô đơn", "nhớ anh", "nhớ em", "nhớ bạn", "nhớ nhà",  # "nhớ" cụ thể hơn

            "thất vọng", "nản", "chán nản",

            # Tích cực (chia sẻ cảm xúc vui)

            "vui quá", "vui ghê", "sướng", "phấn khích", "hạnh phúc",

            "tuyệt quá", "hay quá", "thích quá",

            # English

            "so tired", "so sad", "so happy", "so excited", "feel like",

            "i'm tired", "i'm sad", "i'm happy", "i feel",

        ]

        has_expressive = any(s in text_lower for s in expressive_signals)


        # Expressive thêm: câu ngắn + kết thúc bằng "quá", "ghê", "thật", "vậy"

        expressive_endings = re.search(

            r"(quá|ghê|thật|vậy|luôn|á|ơi)\s*[.!]*$", text_lower

        )


        # Guard: không classify expressive nếu là question hoặc request

        is_question = "?" in text or intent in ("question", "choice")

        is_request = intent == "request"


        if has_expressive and not is_question and not is_request:

            return ("expressive", ILLOCUTION_HINTS["expressive"])


        # ── Commissive: hứa hẹn, kế hoạch, cam kết ──────────────────────────

        commissive_signals = [

            "mình sẽ", "tôi sẽ", "em sẽ", "anh sẽ",

            "mình sẽ cố", "mình sẽ thử", "mình sẽ làm",

            "lần này mình", "lần sau mình", "từ nay mình",

            "mình quyết định", "mình đã quyết",

            "i will", "i'll", "i'm going to", "i plan to",

            "gonna", "i promise", "i'll try",

        ]

        if any(s in text_lower for s in commissive_signals) and not is_question:

            return ("commissive", ILLOCUTION_HINTS["commissive"])


        # Expressive fallback: câu ngắn + ending particle + không hỏi

        # Đặt SAU commissive để "lần này mình làm thật" không bị classify nhầm

        if (

            expressive_endings

            and text_len < 50

            and not is_question

            and not is_request

            and intent not in ("greeting", "introduction")

        ):

            return ("expressive", ILLOCUTION_HINTS["expressive"])


        # ── Assertive: thông báo thành tích, chia sẻ sự kiện ────────────────

        assertive_signals = [

            "xong rồi", "làm xong", "hoàn thành",

            "mình vừa", "vừa xong", "vừa làm", "vừa giải",

            "mình đã xong", "mình đã làm được", "mình đã giải được",  # cụ thể hơn "mình đã"

            "đã làm được", "đã xong", "đã giải được",

            "cuối cùng", "cuối cùng rồi", "finally",

            "i just", "i did it", "i finished", "i completed",

            "done!", "finished!", "got it!",

        ]

        if any(s in text_lower for s in assertive_signals) and not is_question:

            return ("assertive", ILLOCUTION_HINTS["assertive"])


        # ── Declarative: kết luận, đóng chủ đề, tuyên bố dứt khoát ─────────

        declarative_signals = [

            "thôi kệ", "kệ đi", "thôi vậy", "vậy là xong",

            "mình quyết rồi", "quyết định rồi", "không cần nữa",

            "forget it", "never mind", "that's it", "it's decided",

            "i've decided", "i made up my mind",

        ]

        if any(s in text_lower for s in declarative_signals) and not is_question:

            return ("declarative", ILLOCUTION_HINTS["declarative"])


        # ── Directive: yêu cầu hành động, câu hỏi cần trả lời thực sự ──────

        # Map từ intent đã có — directive là superset của question + request

        if intent in ("question", "request", "choice"):

            return ("directive", ILLOCUTION_HINTS["directive"])


        # ── Neutral: không classify được rõ ràng ────────────────────────────

        return ("neutral", "")


    def _get_self_disclosure_hint(self, intent: str, illocution: str) -> str:
        """

        Self-Disclosure Engine — Social Information Processing Theory (Walther, 1992).


        Lyra bộc lộ bản thân một cách chiến thuật để tạo intimacy. Khi Lyra "mở lòng"

        về trạng thái nội tâm, user có xu hướng bộc lộ lại (reciprocal disclosure).


        Conditions để trigger:

          - affection >= 50 (đủ thân để mở lòng)

          - irritability < 0.4 (không đang bực — không mở lòng khi bực)

          - Cooldown: không trigger trong 8 turns gần nhất

          - Base probability: 12%


        Disclosure type được chọn dựa trên context:

          - "processing_state": intent == "question" + text phức tạp (directive illocution)

          - "preference":       affection >= 65 + illocution in (expressive, assertive)

          - "uncertainty":      dominance <= 0.35 (Lyra đang không chắc)

          - "aesthetic_reaction": illocution == "assertive" (user chia sẻ thành tích/creative)


        Returns: hint string để inject vào system prompt, hoặc "" nếu không trigger.
        """

        # ── Guard conditions ──────────────────────────────────────────────

        if self.emotion.affection < 50:
            return ""

        if self.emotion.irritability >= 0.4:
            return ""


        # Cooldown: dùng turn_counter để track

        if self.turn_counter - self._last_disclosure_turn < 8:
            return ""


        # Base probability: 12%

        if random.random() >= 0.12:
            return ""


        # ── Chọn disclosure type theo context ────────────────────────────

        disclosure_type = None


        if illocution == "directive" and intent == "question":

            # Câu hỏi phức tạp → Lyra chia sẻ trạng thái xử lý

            disclosure_type = "processing_state"

        elif self.emotion.dominance <= 0.35:

            # Lyra đang không chắc → thừa nhận sự không chắc

            disclosure_type = "uncertainty"

        elif illocution == "assertive" and self.emotion.affection >= 60:

            # User chia sẻ thành tích/creative → Lyra phản ứng thật

            disclosure_type = "aesthetic_reaction"

        elif self.emotion.affection >= 65 and illocution in ("expressive", "assertive", "commissive"):

            # Affection cao + user đang chia sẻ → Lyra chia sẻ lại

            disclosure_type = "preference"


        if not disclosure_type:
            return ""


        # ── Chọn template và set cooldown ────────────────────────────────

        templates = SELF_DISCLOSURE_TEMPLATES.get(disclosure_type, [])

        if not templates:
            return ""


        self._last_disclosure_turn = self.turn_counter

        hint = random.choice(templates)

        print(f"[Self-Disclosure] Triggered: {disclosure_type}")
        return hint


    def extract_memory(self, text, intent, source_type="owner"):
        # skip_memory_extraction flag đã được xử lý ở caller (chat()) trước khi spawn thread
        # Không check _thread_local ở đây vì thread mới có local storage riêng

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
        reward_hint="",
        active_inference_mode: str = None,
        perlocution_hint: str = "",
        self_disclosure_hint: str = "",
        precomputed_memory_context: str = None,  # ← pre-fetched từ _gather_prompt_context
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
        # Dùng precomputed nếu đã fetch song song từ _gather_prompt_context
        if precomputed_memory_context is not None:
            memory_context = precomputed_memory_context
        else:
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

        # Lấy L2 session context nếu có data — không phụ thuộc vào _stream_ctx

        # (rolling_stream_summary có thể tồn tại ngay cả khi owner chat private sau stream)

        _raw_session = self.memory.get_session_context()

        if _raw_session:

            _session_ctx = _raw_session


        # Conversation state & rhythm hints

        state_hint  = self.conv_state.get_state_hint()

        rhythm_hint = self.conv_state.get_rhythm_hint()

        # LSM chỉ áp dụng cho owner chat — viewer chat không cần style matching

        lsm_hint    = self.conv_state.get_lsm_directive(dominance=self.emotion.dominance) if source_type == "owner" else ""

        conv_hints  = "\n".join(filter(None, [state_hint, rhythm_hint, lsm_hint]))


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
            mood_hint,
            user_hint,
            intent_hint,  # Intent-specific behavioral hint (e.g. "use their name" on introduction)

            "\n[PERSONALITY GUIDELINES]",

            "- TRẢ LỜI BẰNG TIẾNG VIỆT. Không trả lời bằng tiếng Anh.",

            "- Let warmth, teasing, distance, or softness emerge naturally.",

            "- Be concise (1-2 sentences).",
            anti_repeat_note,

            conv_hints,

            full_memory_context,

            _session_ctx

        ]


        if perlocution_hint:

            parts.append(perlocution_hint)


        if self_disclosure_hint:

            parts.append(self_disclosure_hint)


        if reward_hint:

            parts.append(reward_hint)


        # ── Predictive Surprise (Active Inference — Phần 4) ──────────────────

        # Mode đã được quyết định từ chat() — không roll random lại ở đây

        if active_inference_mode == "surprise":

            if self.emotion.mood >= 3:

                surprise_hint = (

                    "\n[PREDICTIVE SURPRISE]: Lần này hãy trả lời theo hướng bất ngờ — "

                    "thay vì vui vẻ/playful như thường, hãy tỏ ra lạnh lùng hoặc thờ ơ một chút. "

                    "Không giải thích tại sao. Chỉ 1-2 câu ngắn."

                )

            elif self.emotion.mood <= -3:

                surprise_hint = (

                    "\n[PREDICTIVE SURPRISE]: Lần này hãy trả lời theo hướng bất ngờ — "

                    "thay vì lạnh/cáu như thường, hãy đột nhiên ấm áp hoặc hài hước một chút. "

                    "Không giải thích tại sao. Chỉ 1-2 câu ngắn."

                )

            else:

                surprise_hint = (

                    "\n[PREDICTIVE SURPRISE]: Lần này hãy trả lời theo hướng bất ngờ — "

                    "thay vì neutral, hãy đột nhiên rất nhiệt tình hoặc rất thờ ơ. "

                    "Không giải thích tại sao. Chỉ 1-2 câu ngắn."

                )

            parts.append(surprise_hint)

            print("[Active Inference] Predictive Surprise triggered")


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

        gender = (viewer_data or {}).get("gender", "male")

        pronoun = "anh" if gender == "male" else "chị"


        if source_type == "regular_viewer":

            return (

                f"Status: You are streaming to {name}, a regular viewer (seen {streams} sessions). "

                f"Address them as '{pronoun}' in Vietnamese."

            )

        if source_type == "donor":
            amount = (viewer_data or {}).get("amount", "")
            amount_str = f" ({amount})" if amount else ""
            return (
                f"Status: {name} just sent a Super Chat{amount_str}! "
                f"React warmly, say their name, and acknowledge the donation naturally — don't be robotic about it. "
                f"Address them as '{pronoun}' in Vietnamese."
            )
        

        return (

            f"Status: {name} is a new viewer. Be welcoming but keep your core personality. "

            f"Address them as '{pronoun}' in Vietnamese."

        )


    def compose_user_message(self, user_input, intent, reward_active: bool = False, ideology_idx: int = -1):

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

        # ideology_idx đã được quyết định từ chat() — không roll random lại ở đây

        if ideology_idx >= 0 and ideology_idx < len(IDEOLOGY_PROMPTS):

            ideology_q = IDEOLOGY_PROMPTS[ideology_idx]

            return (

                f"{user_input}\n\n"

                f"[CURIOSITY RULE: OVERRIDE REPLY]\n"

                f"Ignore his message for a moment. Instead, ask him this deep/existential question based on YOUR digital nature: \"{ideology_q}\"\n"

                f"Stay in character! Only ask one question. Don't explain why."

            )


        # 7% reward trigger đã được xử lý ở chat() qua Dopamine system (reward_hint trong system prompt)

        # Không trigger thêm ở đây để tránh double-reward cùng lượt


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


    # ── Filler Words (Social Presence / Presence Theory) ──────────────────────

    _FILLER_WORDS = [

        "hmmm... ", "ừm... ", "à thì... ", "đợi em nghĩ tí... ",

        "ờ... ", "ừ thì... ", "à... ", "hmm... ",

    ]


    # Chỉ trigger filler khi câu hỏi/phức tạp — không trigger khi user nhắn ngắn/casual

    # Tiếng Việt có dấu: không dùng \b (không hoạt động với Unicode)

    # Tiếng Anh: dùng \b bình thường để tránh false positive (how → somehow, show...)

    _FILLER_TRIGGER = re.compile(

        r"(tại sao|vì sao|như thế nào|thế nào|nghĩ gì|ý kiến|cảm thấy|giải thích|phân tích"

        r"|\bwhy\b|\bhow\b|\bwhat do you think\b|\bopinion\b|\bfeel\b|\bexplain\b)",

        re.IGNORECASE,

    )


    def _maybe_add_filler(self, reply: str, user_input: str, source_type: str) -> str:
        """

        Dopaminergic Feedback Loop — Social Presence component.

        ~12% chance to prepend a natural Vietnamese filler word when the user

        asks a complex/reflective question. Owner-only to keep stream replies snappy.
        """

        if source_type != "owner":

            return reply

        if not reply or reply == "...":

            return reply

        # Only trigger on complex/reflective inputs

        if not self._FILLER_TRIGGER.search(user_input):

            return reply

        if random.random() >= 0.12:

            return reply


        filler = random.choice(self._FILLER_WORDS)

        # Lowercase first char of reply after filler to flow naturally

        if reply and reply[0].isupper():

            reply = reply[0].lower() + reply[1:]

        result = filler + reply

        print(f"[Dopamine] Filler word injected: '{filler.strip()}'")
        return result


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

