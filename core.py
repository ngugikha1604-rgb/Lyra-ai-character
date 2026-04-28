import os
import re
import json
import random
import threading
import concurrent.futures
from datetime import datetime

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

# Internal Imports
from config import *
from live_context import maybe_refresh_from_emotion
from background_worker import enqueue, PRIORITY_CRITICAL, PRIORITY_HIGH
from time_utils import (
    get_vietnam_time, get_time_period, calculate_time_gap, 
    should_send_greeting, get_time_context
)
from emotion import EmotionEngine
from memory import MemorySystem
from vbrain import parse_vbrain_response
from conversation_state import ConversationStateDetector
from skill_synthesizer import SkillSynthesizer
from prompts import REWARD_HINTS, IDEOLOGY_PROMPTS
from rl_feedback_loop import RLFeedbackLoop

# Mixins
from behavioral_logic import BehavioralMixin
from prompt_builder import PromptBuilderMixin
from stream_handler import StreamHandlerMixin
from memory_handler import MemoryHandlerMixin
from model_utilities import ModelUtilityMixin

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class MiniAI(BehavioralMixin, PromptBuilderMixin, StreamHandlerMixin, MemoryHandlerMixin, ModelUtilityMixin):
    """
    Lyra AI Core Engine.
    Refactored using Mixins to maintain a clean, modular structure.
    """

    def __init__(self):
        self.model = CHAT_MODEL
        self.timeout = 45
        self.headers = {"Content-Type": "application/json"}
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
        self._thread_local = threading.local()
        
        self.memory.load()
        self.rl_loop = RLFeedbackLoop(self)
        self.is_streaming = False
        self.stream_turn_counter = 0
        self._last_viewer_message_time = None
        
        self.messages = self.memory.memory.get("conversation", {}).get("conversation_thread", [])
        self.recent_responses = []
        self.last_intent = None
        self._user_mood_today = None
        self._last_disclosure_turn = 0
        
        self.emotion.affection = self.memory.memory.get("relationship", {}).get("current_affection", 50)
        self.current_time = get_vietnam_time()
        self.time_period = get_time_period(self.current_time.hour)

        self.skills_dir = os.path.join(BASE_DIR, "skills")
        self.synthesizer = SkillSynthesizer(self.skills_dir)
        self._skills_index = self._load_skill_index()
        
        self.last_message_time = self.memory.memory.get("time_tracking", {}).get("last_message_time")
        self.time_gap_hours = calculate_time_gap(self.last_message_time, self.current_time)
        self.should_greet = should_send_greeting(self.time_gap_hours, self.last_message_time)

        print("[Core] Pre-loading embedding model...")
        self.memory._get_embedding("init")
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    @property
    def turn_counter(self):
        return self.memory.memory["conversation"].get("total_messages", 0)

    @turn_counter.setter
    def turn_counter(self, value):
        self.memory.memory["conversation"]["total_messages"] = value

    @property
    def attention(self): return self.emotion.attention
    @property
    def mood(self): return self.emotion.mood
    @property
    def affection(self): return self.emotion.affection
    @property
    def memory_dict(self): return self.memory.memory

    def chat(self, user_input, source_type: str = "owner", viewer_data: dict = None, stream_context: str = ""):
        """Main orchestration loop for Lyra's conversation."""
        self.current_time = get_vietnam_time()
        self.time_period = get_time_period(self.current_time.hour)
        self.time_gap_hours = calculate_time_gap(self.last_message_time, self.current_time)
        self.should_greet = should_send_greeting(self.time_gap_hours, self.last_message_time)

        self.turn_counter += 1
        intent = self.detect_intent(user_input)

        # Behavioral & Psychological Signals
        _illocution_type, _perlocution_hint = self.classify_illocution(user_input, intent) if source_type == "owner" else ("neutral", "")
        self.emotion.update(user_input, self.time_gap_hours, intent=intent)

        if source_type == "owner" and self.is_streaming:
            enqueue(PRIORITY_HIGH, maybe_refresh_from_emotion, self.emotion.get_state())

        _self_disclosure_hint = self._get_self_disclosure_hint(intent, _illocution_type) if source_type == "owner" else ""

        # Stream Monitoring
        if source_type != "owner" and stream_context:
            self.stream_turn_counter += 1
            viewer_name = (viewer_data or {}).get("viewer_name", "")
            if viewer_name:
                self.memory.add_session_item(f"{viewer_name} nhắn: {user_input[:80]}", kind="session")
            if self.stream_turn_counter % STREAM_SUMMARY_THRESHOLD == 0:
                enqueue(PRIORITY_HIGH, self.update_stream_summary)

        # Context Gathering (Parallel)
        _original_affection = self.emotion.affection
        _original_dominance = self.emotion.dominance
        if source_type != "owner":
            self.emotion.affection = float((viewer_data or {}).get("affection", 10))
        
        self.conv_state.update(user_input, self.messages)
        enqueue(PRIORITY_NORMAL, self.summarize_history)
        
        _memory_future = self._executor.submit(self.memory.get_relevant_context, user_input, source_type != "owner")
        _needs_search, _search_query = self._should_search(user_input) if source_type == "owner" else (False, None)
        _search_future = self._executor.submit(self._search_web, _search_query) if _needs_search else None

        _precomputed_memory = _memory_future.result() or ""
        search_context = f"\n\n[SEARCH RESULTS]\n{_search_future.result()}\n" if _search_future else ""

        # Reward System (Skinner)
        reward_hint = ""
        if source_type == "owner":
            reward_type = self.conv_state.should_trigger_reward(0.07)
            if reward_type == "deep_recall":
                rare_mem = self.memory.get_rare_memory()
                if rare_mem:
                    reward_hint = random.choice(REWARD_HINTS["deep_recall"]).format(memory=rare_mem)
                    self.conv_state.confirm_reward_delivered()
                else: reward_type = "healthy_debate"
            
            if reward_type and reward_type in REWARD_HINTS and not reward_hint:
                reward_hint = random.choice(REWARD_HINTS[reward_type])
                self.conv_state.confirm_reward_delivered()

        if reward_hint: _self_disclosure_hint = ""

        # Active Inference
        active_inference_mode, _ideology_idx = None, -1
        if source_type == "owner" and not reward_hint:
            if self.attention >= 4:
                _ideology_idx = self.conv_state.should_trigger_ideology(len(IDEOLOGY_PROMPTS))
            if _ideology_idx >= 0:
                active_inference_mode = "ideology"
            elif self.conv_state.should_trigger_surprise(0.05):
                active_inference_mode = "surprise"

        if active_inference_mode: _self_disclosure_hint = ""

        # Prompt Building & API Call
        system_prompt = self.build_prompt(
            intent, user_input, search_context, source_type, viewer_data, stream_context,
            reward_hint=reward_hint, active_inference_mode=active_inference_mode,
            perlocution_hint=_perlocution_hint, self_disclosure_hint=_self_disclosure_hint,
            precomputed_memory_context=_precomputed_memory
        )
        composed = self.compose_user_message(user_input, intent, bool(reward_hint), _ideology_idx)
        
        api_messages = [{"role": "system", "content": system_prompt}]
        history_window = MAX_HISTORY * 2 if source_type == "owner" else 8
        api_messages.extend(self.messages[-history_window:])
        api_messages.append({"role": "user", "content": composed})

        dynamic_max_tokens = self.conv_state.get_pace_max_tokens(self.emotion.get_dynamic_max_tokens())
        dynamic_temp = self.conv_state.get_temperature(self.emotion.mood, self.emotion.attention, self.emotion.dominance)

        content = self._call_model(api_messages, temperature=dynamic_temp, max_tokens=dynamic_max_tokens)
        if content:
            parsed = parse_vbrain_response(content)
            # Skill Check
            if parsed.get("skill_needed"):
                skill_content = self._load_skill_content(parsed["skill_needed"])
                if skill_content:
                    self._log_skill_usage(parsed["skill_needed"])
                    api_messages[0]["content"] = self.build_prompt(
                        intent, user_input, search_context, source_type, viewer_data, stream_context,
                        loaded_skill_content=skill_content, reward_hint=reward_hint,
                        perlocution_hint=_perlocution_hint, self_disclosure_hint=_self_disclosure_hint,
                        precomputed_memory_context=_precomputed_memory
                    )
                    content = self._call_model(api_messages, temperature=dynamic_temp, max_tokens=dynamic_max_tokens)
                    if content: parsed = parse_vbrain_response(content)
            self.current_vbrain = parsed

            # --- THOUGHT CHAINING (Inner Monologue Continuation) ---
            # Section 11: If monologue > 20 chars and random() < 0.07, call model a second time.
            if random.random() < 0.07:
                monologue = parsed.get("monologue", "")
                if len(monologue) > 20:
                    print(f"[Core] Thought Chaining triggered (len={len(monologue)}).")
                    chain_messages = [
                        {"role": "system", "content": THOUGHT_CHAIN_SYSTEM},
                        {"role": "user", "content": f"[INNER MONOLOGUE]: {monologue}"}
                    ]
                    chain_content = self._call_model(chain_messages, temperature=0.5, max_tokens=150)
                    if chain_content:
                        chain_parsed = parse_vbrain_response(chain_content)
                        second_monologue = chain_parsed.get("monologue", "")
                        if second_monologue:
                            # Prepend the second response's monologue to the original one
                            parsed["monologue"] = f"{second_monologue}\n\n[Original]: {monologue}"
                            # Refine the reply/emotion/action using the second response
                            parsed["reply"] = chain_parsed.get("reply", parsed.get("reply"))
                            parsed["emotion"] = chain_parsed.get("emotion", parsed.get("emotion"))
                            parsed["action"] = chain_parsed.get("action", parsed.get("action"))
                            print("[Core] Thought Chaining completed.")
                            self.current_vbrain = parsed
        else:
            self.current_vbrain = {"monologue": "", "emotion": "neutral", "action": "NONE", "reply": "..."}

        reply = self.clean_reply(self.current_vbrain.get("reply", "..."))
        original_reply = reply
        reply = self._maybe_add_filler(reply, user_input, source_type)
        
        if source_type != "owner":
            self.emotion.affection, self.emotion.dominance = _original_affection, _original_dominance

        # Background Tasks
        if not getattr(self._thread_local, "skip_memory_extraction", False):
            enqueue(PRIORITY_CRITICAL, self.extract_memory, user_input, intent, source_type)
        else: self._thread_local.skip_memory_extraction = False

        if source_type == "owner":
            self.messages.append({"role": "user", "content": user_input})
            self.messages.append({"role": "assistant", "content": original_reply})
            self.memory.memory.setdefault("conversation", {}).setdefault("conversation_thread", []).extend([
                {"role": "user", "content": user_input}, {"role": "assistant", "content": original_reply}
            ])
            self.turn_counter += 2  # user + assistant
            self.recent_responses.append(original_reply.lower()[:30])
            if len(self.recent_responses) > 10: self.recent_responses.pop(0)
            
            self.last_message_time = self.current_time.isoformat()
            self.memory.memory["time_tracking"]["last_message_time"] = self.last_message_time
            enqueue(PRIORITY_NORMAL, self.memory.save)

            if self.turn_counter % 25 == 0:
                enqueue(PRIORITY_NORMAL, self.synthesizer.synthesize, self.messages[:], self)

        result_dict = {
            "reply": reply, "original_reply": original_reply, "monologue": self.current_vbrain.get("monologue", ""),
            "emotion": self.current_vbrain.get("emotion", "neutral"), "action": self.current_vbrain.get("action", "NONE"),
            "mood": self.emotion.mood, "affection": self.emotion.affection, "dominance": round(self.emotion.dominance, 2),
            "vad": self.emotion.get_vad(), "intent": intent, "illocution": _illocution_type, "conv_state": self.conv_state.state
        }

        # Reinforcement Learning: Track action for reward if it's a stream chat
        if source_type != "owner":
            self.rl_loop.register_action(reply, intent, self.emotion.get_state())

        return result_dict

    def emotion_from_state(self): return self.emotion.emotion_from_state()

    def _load_skill_index(self):
        if os.path.exists(self.synthesizer.index_path):
            try:
                with open(self.synthesizer.index_path, "r", encoding="utf-8") as f:
                    return f.read()
            except: pass
        return ""

    def _load_skill_content(self, skill_name):
        return self.synthesizer.get_skill_context(skill_name)

    def _log_skill_usage(self, skill_name):
        import time
        print(f"[Skill] Using: {skill_name}")
        stats_path = self.synthesizer.stats_path
        try:
            stats = {}
            if os.path.exists(stats_path):
                with open(stats_path, "r", encoding="utf-8") as f:
                    stats = json.load(f)
            if skill_name not in stats:
                stats[skill_name] = {"call_count": 0, "last_used": 0}
            stats[skill_name]["call_count"] += 1
            stats[skill_name]["last_used"] = time.time()
            with open(stats_path, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
        except: pass

    def get_proactive_message(self):
        """Generates a proactive message based on time and situation."""
        from prompts import PROACTIVE_TIME_TEMPLATES
        situation = "morning" if self.time_period == "morning" else "generic"
        template = random.choice(PROACTIVE_TIME_TEMPLATES.get(situation, ["..."]))
        return self._call_light_model([{"role": "user", "content": template}], temperature=0.7)
