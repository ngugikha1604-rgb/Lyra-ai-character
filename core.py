import os
import re
import json
import random
import threading
import concurrent.futures

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

# Internal Imports
from config import *
from live_context import maybe_refresh_from_emotion
from background_worker import enqueue, PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_NORMAL
from time_utils import get_vietnam_time, get_time_period, calculate_time_gap, should_send_greeting
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

class MiniAI(
    BehavioralMixin,
    PromptBuilderMixin,
    StreamHandlerMixin,
    MemoryHandlerMixin,
    ModelUtilityMixin,
):
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
        return self.memory.memory

    def _refresh_time_state(self):
        self.current_time = get_vietnam_time()
        self.time_period = get_time_period(self.current_time.hour)
        self.time_gap_hours = calculate_time_gap(self.last_message_time, self.current_time)
        self.should_greet = should_send_greeting(self.time_gap_hours, self.last_message_time)

    def _owner_behavior_hints(self, user_input, intent, source_type):
        if source_type != "owner":
            return "neutral", "", ""

        illocution, perlocution = self.classify_illocution(user_input, intent)
        disclosure = self._get_self_disclosure_hint(intent, illocution)
        return illocution, perlocution, disclosure

    def _track_stream_turn(self, user_input, source_type, viewer_data, stream_context):
        if source_type == "owner" or not stream_context:
            return

        self.stream_turn_counter += 1
        viewer_name = (viewer_data or {}).get("viewer_name", "")
        if viewer_name:
            self.memory.add_session_item(f"{viewer_name} nhắn: {user_input[:80]}", kind="session")
        if self.stream_turn_counter % STREAM_SUMMARY_THRESHOLD == 0:
            enqueue(PRIORITY_HIGH, self.update_stream_summary)

    def _apply_viewer_emotion_context(self, source_type, viewer_data):
        original_state = (self.emotion.affection, self.emotion.dominance)
        if source_type != "owner":
            self.emotion.affection = float((viewer_data or {}).get("affection", 10))
        return original_state

    def _restore_viewer_emotion_context(self, source_type, original_state):
        if source_type != "owner":
            self.emotion.affection, self.emotion.dominance = original_state

    def _collect_turn_context(self, user_input, source_type):
        self.conv_state.update(user_input, self.messages)
        enqueue(PRIORITY_NORMAL, self.summarize_history)

        is_public = source_type != "owner"
        memory_future = self._executor.submit(self.memory.get_relevant_context, user_input, is_public)
        needs_search, search_query = self._should_search(user_input) if source_type == "owner" else (False, None)
        search_future = self._executor.submit(self._search_web, search_query) if needs_search else None

        memory_context = memory_future.result() or ""
        search_context = ""
        if search_future:
            search_result = search_future.result()
            if search_result:
                search_context = f"\n\n[SEARCH RESULTS]\n{search_result}\n"
        return memory_context, search_context

    def _build_reward_hint(self, source_type):
        if source_type != "owner":
            return ""

        reward_type = self.conv_state.should_trigger_reward(0.07)
        reward_hint = ""

        if reward_type == "deep_recall":
            rare_mem = self.memory.get_rare_memory()
            if rare_mem:
                reward_hint = random.choice(REWARD_HINTS["deep_recall"]).format(memory=rare_mem)
                self.conv_state.confirm_reward_delivered()
            else:
                reward_type = "healthy_debate"

        if reward_type and reward_type in REWARD_HINTS and not reward_hint:
            reward_hint = random.choice(REWARD_HINTS[reward_type])
            self.conv_state.confirm_reward_delivered()

        return reward_hint

    def _choose_active_inference(self, source_type, reward_hint):
        active_mode, ideology_idx = None, -1
        if source_type != "owner" or reward_hint:
            return active_mode, ideology_idx

        if self.attention >= 4:
            ideology_idx = self.conv_state.should_trigger_ideology(len(IDEOLOGY_PROMPTS))

        if ideology_idx >= 0:
            active_mode = "ideology"
        elif self.conv_state.should_trigger_surprise(0.05):
            active_mode = "surprise"

        return active_mode, ideology_idx

    def _build_api_messages(self, system_prompt, composed_message, source_type):
        history_window = MAX_HISTORY * 2 if source_type == "owner" else 8
        return [
            {"role": "system", "content": system_prompt},
            *self.messages[-history_window:],
            {"role": "user", "content": composed_message},
        ]

    def _call_and_parse_vbrain(self, api_messages, dynamic_temp, dynamic_max_tokens, prompt_args):
        content = self._call_model(api_messages, temperature=dynamic_temp, max_tokens=dynamic_max_tokens)
        if not content:
            return {"monologue": "", "emotion": "neutral", "action": "NONE", "reply": "..."}

        parsed = parse_vbrain_response(content)
        skill_name = parsed.get("skill_needed")
        if not skill_name:
            return parsed

        skill_content = self._load_skill_content(skill_name)
        if not skill_content:
            return parsed

        self._log_skill_usage(skill_name)
        api_messages[0]["content"] = self.build_prompt(
            *prompt_args["base"],
            loaded_skill_content=skill_content,
            reward_hint=prompt_args["reward_hint"],
            active_inference_mode=prompt_args["active_inference_mode"],
            perlocution_hint=prompt_args["perlocution_hint"],
            self_disclosure_hint=prompt_args["self_disclosure_hint"],
            precomputed_memory_context=prompt_args["memory_context"],
        )
        content = self._call_model(api_messages, temperature=dynamic_temp, max_tokens=dynamic_max_tokens)
        return parse_vbrain_response(content) if content else parsed

    def _enqueue_memory_extraction(self, user_input, intent, source_type):
        if getattr(self._thread_local, "skip_memory_extraction", False):
            self._thread_local.skip_memory_extraction = False
            return
        enqueue(PRIORITY_CRITICAL, self.extract_memory, user_input, intent, source_type)

    def _persist_owner_turn(self, user_input, original_reply):
        turn_rows = [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": original_reply},
        ]
        self.messages.extend(turn_rows)
        self.memory.memory.setdefault("conversation", {}).setdefault("conversation_thread", []).extend(turn_rows)
        self.memory.queue_conversation_row("user", user_input)
        self.memory.queue_conversation_row("assistant", original_reply)
        self.turn_counter += 1  # assistant; user was counted at turn start

        self.recent_responses.append(original_reply.lower()[:30])
        if len(self.recent_responses) > 10:
            self.recent_responses.pop(0)

        self.last_message_time = self.current_time.isoformat()
        self.memory.memory.setdefault("relationship", {})["current_affection"] = int(round(self.emotion.affection))
        self.memory.memory["time_tracking"]["last_message_time"] = self.last_message_time
        enqueue(PRIORITY_NORMAL, self.memory.save)

    def _build_chat_result(self, reply, original_reply, intent, illocution):
        return {
            "reply": reply,
            "original_reply": original_reply,
            "monologue": self.current_vbrain.get("monologue", ""),
            "emotion": self.current_vbrain.get("emotion", "neutral"),
            "action": self.current_vbrain.get("action", "NONE"),
            "mood": self.emotion.mood,
            "affection": self.emotion.affection,
            "dominance": round(self.emotion.dominance, 2),
            "vad": self.emotion.get_vad(),
            "intent": intent,
            "illocution": illocution,
            "conv_state": self.conv_state.state,
        }

    def chat(self, user_input, source_type: str = "owner", viewer_data: dict = None, stream_context: str = ""):
        """Main orchestration loop for Lyra's conversation."""
        self._refresh_time_state()
        self.turn_counter += 1

        intent = self.detect_intent(user_input)
        illocution, perlocution_hint, disclosure_hint = self._owner_behavior_hints(
            user_input, intent, source_type
        )
        self.emotion.update(user_input, self.time_gap_hours, intent=intent)

        if source_type == "owner" and self.is_streaming:
            enqueue(PRIORITY_HIGH, maybe_refresh_from_emotion, self.emotion.get_state())

        self._track_stream_turn(user_input, source_type, viewer_data, stream_context)

        if self.turn_counter % REFLECTION_INTERVAL == 0:
            enqueue(PRIORITY_NORMAL, self._reflect_on_session)

        original_emotion_state = self._apply_viewer_emotion_context(source_type, viewer_data)
        memory_context, search_context = self._collect_turn_context(user_input, source_type)

        reward_hint = self._build_reward_hint(source_type)
        if reward_hint:
            disclosure_hint = ""

        active_inference_mode, ideology_idx = self._choose_active_inference(source_type, reward_hint)
        if active_inference_mode:
            disclosure_hint = ""

        prompt_base = (intent, user_input, search_context, source_type, viewer_data, stream_context)
        system_prompt = self.build_prompt(
            *prompt_base,
            reward_hint=reward_hint, active_inference_mode=active_inference_mode,
            perlocution_hint=perlocution_hint, self_disclosure_hint=disclosure_hint,
            precomputed_memory_context=memory_context
        )
        composed = self.compose_user_message(user_input, intent, bool(reward_hint), ideology_idx)
        api_messages = self._build_api_messages(system_prompt, composed, source_type)

        dynamic_max_tokens = self.conv_state.get_pace_max_tokens(self.emotion.get_dynamic_max_tokens())
        dynamic_temp = self.conv_state.get_temperature(self.emotion.mood, self.emotion.attention, self.emotion.dominance)

        prompt_args = {
            "base": prompt_base,
            "reward_hint": reward_hint,
            "active_inference_mode": active_inference_mode,
            "perlocution_hint": perlocution_hint,
            "self_disclosure_hint": disclosure_hint,
            "memory_context": memory_context,
        }
        self.current_vbrain = self._call_and_parse_vbrain(
            api_messages, dynamic_temp, dynamic_max_tokens, prompt_args
        )

        reply = self.clean_reply(self.current_vbrain.get("reply", "..."))
        original_reply = reply
        reply = self._maybe_add_filler(reply, user_input, source_type)

        self._restore_viewer_emotion_context(source_type, original_emotion_state)
        self._enqueue_memory_extraction(user_input, intent, source_type)

        if source_type == "owner":
            self._persist_owner_turn(user_input, original_reply)

        result_dict = self._build_chat_result(reply, original_reply, intent, illocution)
        if source_type != "owner":
            self.rl_loop.register_action(user_input, reply, intent, self.emotion.get_state())

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

    def _reflect_on_session(self):
        """Mid-session reflection loop to generate high-level insights."""
        try:
            print("[Core] Running reflection loop...")
            # 1. Gather context
            recent_convo = "\n".join([f"{'User' if m['role'] == 'user' else 'Lyra'}: {m['content']}" for m in self.messages[-20:]])
            session_items = "\n".join([f"- {i['value']}" for i in self.memory._session_items[-10:]])
            emotion_state = self.emotion.describe_internal_state()
            
            prompt = (
                f"Bạn là tiềm thức của Lyra. Hãy tự suy ngẫm về diễn biến gần đây.\n\n"
                f"Lịch sử chat gần đây:\n{recent_convo}\n\n"
                f"Diễn biến session (L2):\n{session_items}\n\n"
                f"Trạng thái cảm xúc: {emotion_state}\n\n"
                f"Nhiệm vụ: Tóm tắt 2-3 'Key Insights' (thấu hiểu) về tình hình hiện tại (ví dụ: User đang bận, Viewer thích đùa nhây, Lyra đang mệt). "
                f"Trả về JSON cực ngắn: {{\"insights\": [\"insight 1\", \"insight 2\"]}}"
            )
            
            raw = self._call_light_model([
                {"role": "system", "content": "Bạn là subconscious của Lyra. Chỉ trả về JSON."},
                {"role": "user", "content": prompt}
            ], temperature=0.3, max_tokens=150)
            
            if raw:
                match = re.search(r'\{.*\}', raw, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    insights = data.get("insights", [])
                    if insights:
                        from live_context import update_insights
                        update_insights(insights)
                        print(f"[Reflection] New insights: {insights}")
                        
                        # Feature 3: Auto-update plan if exists
                        self._update_plan_status(insights)

        except Exception as e:
            print(f"[Core] Reflection loop error: {e}")

    def _update_plan_status(self, insights):
        """Updates the status of plan items based on recent insights."""
        from live_context import load_live_context, update_plan
        lc = load_live_context()
        plan = lc.get("stream_plan", [])
        if not plan: return
        
        updated = False
        insight_text = " ".join(insights).lower()
        
        # Simple heuristic to mark goals as done based on insights
        for item in plan:
            if item["status"] == "pending":
                # If any insight word matches plan goal keywords
                keywords = re.findall(r"\w{4,}", item["goal"].lower())
                if any(kw in insight_text for kw in keywords if kw not in ("chủ", "nhân", "lyra", "người", "khán", "giả")):
                    item["status"] = "done"
                    updated = True
                    print(f"[Plan] Goal marked as done: {item['goal']}")
        
        if updated:
            update_plan(plan)

    def get_proactive_message(self):
        """Generates a proactive message based on time and situation."""
        from prompts import PROACTIVE_TIME_TEMPLATES
        situation = "morning" if self.time_period == "morning" else "generic"
        template = random.choice(PROACTIVE_TIME_TEMPLATES.get(situation, ["..."]))
        return self._call_light_model([{"role": "user", "content": template}], temperature=0.7)
