import os
import re
import json
import random
import threading
import concurrent.futures
import time

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

# Internal Imports
from config import *
from live_context import maybe_refresh_from_emotion
from background_worker import enqueue, PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_NORMAL
from time_utils import get_vietnam_time, get_time_period, calculate_time_gap, should_send_greeting
from emotion import EmotionEngine
from memory import MemorySystem
from vbrain import parse_vbrain_response, validate_emotion, validate_action
from conversation_state import ConversationStateDetector
from skill_synthesizer import SkillSynthesizer
from prompts import REWARD_HINTS, IDEOLOGY_PROMPTS
from rl_feedback_loop import RLFeedbackLoop

# DSPy Integration
import dspy
from dspy_modules.brain_module import LyraBrain


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
        self._skill_loop_count = 0  
        self._vts_loop_count = 0  # Limit VTS feedback loops
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
        self.skills_index = self.synthesizer.load_skill_index() # Use synthesizer method
        
        self.last_message_time = self.memory.memory.get("time_tracking", {}).get("last_message_time")
        self.time_gap_hours = calculate_time_gap(self.last_message_time, self.current_time)
        self.should_greet = should_send_greeting(self.time_gap_hours, self.last_message_time)

        print("[Core] Pre-loading embedding model...")
        self.memory._get_embedding("init")
        
        # DSPy Setup - Use 9router for all LLM calls
        print("[Core] Initializing DSPy Brain via 9router...")
        try:
            # Sử dụng 9router làm central router cho DSPy/LiteLLM
            # Format: "openai/provider/model" để LiteLLM hiểu protocol và 9router route đúng
            # LiteLLM dùng "openai/" prefix để biết dùng OpenAI-compatible protocol
            # Phần sau "openai/" là model string gửi tới 9router: "groq/llama-..."
            # 9router đọc "groq/" để biết route tới Groq provider
            self.dspy_lm = dspy.LM(
                f"openai/groq/{TRANSLATE_MODEL}",
                api_base=ROUTER9_BASE_URL,
                api_key=ROUTER9_API_KEY or "router9-local"
            )
            print(f"[Core] DSPy using 9router → model: groq/{TRANSLATE_MODEL}")
            
            dspy.configure(lm=self.dspy_lm)
            
            self.brain = LyraBrain()
            compiled_path = os.path.join(BASE_DIR, "lyra_compiled.json")
            if os.path.exists(compiled_path):
                self.brain.load(compiled_path)
                print(f"[Core] Loaded compiled brain from {compiled_path}")
            else:
                print("[Core] No compiled brain found, using base module")
        except Exception as e:
            print(f"[Core] DSPy Initialization failed: {e}")
            self.brain = None

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

    def _load_skill_content(self, skill_name):
        """Delegate to synthesizer."""
        return self.synthesizer._load_skill_content(skill_name)

    def _log_skill_usage(self, skill_name):
        """Delegate to synthesizer."""
        return self.synthesizer._log_skill_usage(skill_name)

    def _owner_behavior_hints(self, user_input, intent, source_type):
        if source_type != "owner":
            return "neutral", "", ""

        illocution, perlocution = self.classify_illocution(user_input, intent)
        disclosure = self._get_self_disclosure_hint(intent, illocution)
        return illocution, perlocution, disclosure

    def _track_stream_turn(self, user_input, source_type, viewer_data, stream_context):
        if source_type == "owner" or not stream_context:
            if source_type == "owner":
                # Key Decision Detection (Plandex logic)
                is_decision = any(w in user_input.lower() for w in ["thống nhất", "chốt", "kế hoạch", "nhớ nhé", "agree", "decided"])
                self.memory.add_session_item(f"Anh nói: {user_input[:100]}", kind="owner_input", is_sticky=is_decision)
            return

        self.stream_turn_counter += 1
        viewer_name = (viewer_data or {}).get("viewer_name", "")
        if viewer_name:
            # Viewer profile check (if affection > 30, consider it more sticky/important)
            is_important = float((viewer_data or {}).get("affection", 0)) > 30
            self.memory.add_session_item(f"{viewer_name} nhắn: {user_input[:80]}", kind="session", is_sticky=is_important)
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
                search_context = f"\n\nKẾT QUẢ TÌM KIẾM:\n{search_result}\n"
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

        # Guard: không trigger khi user đang buồn/stress/tức — không phù hợp để hỏi triết học
        _blocked_moods = {"sad", "stressed", "anxious", "frustrated"}
        _current_user_mood = getattr(self, "_last_user_mood", "neutral")
        _in_closing = self.conv_state.state in ("closing", "goodbye")
        if _current_user_mood in _blocked_moods or _in_closing:
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
        # Fallback: Convert dict prompt to string for standard LLM APIs
        system_str = self._dict_to_prompt_string(system_prompt)
        return [
            {"role": "system", "content": system_str},
            *self.messages[-history_window:],
            {"role": "user", "content": composed_message},
        ]

    def _dict_to_prompt_string(self, prompt_dict: dict) -> str:
        """Converts structured prompt dict back to a monolithic string for fallback LLM calls."""
        if not isinstance(prompt_dict, dict): return str(prompt_dict)
        return "\n".join([f"{k.upper()}:\n{v}" for k, v in prompt_dict.items() if v])

    def _get_brain_inputs(self, structured_prompt):
        persona = structured_prompt["persona"]
        situation = structured_prompt["situation"]
        memory = structured_prompt["memory"]
        if structured_prompt.get("behavior_hints"):
            situation += f"\nBEHAVIOR_HINTS:\n{structured_prompt['behavior_hints']}"
        return persona, situation, memory

    def _call_brain(self, structured_prompt, history_str, user_msg):
        persona, situation, memory = self._get_brain_inputs(structured_prompt)
        return self.brain(
            persona=persona,
            situation=situation,
            memory=memory,
            chat_history=history_str,
            user_message=user_msg,
        )

    def _parse_brain_result(self, result):
        skill_needed = getattr(result, "skill_needed", "NONE")
        if skill_needed and skill_needed.upper() == "NONE":
            skill_needed = None
        return {
            "monologue": getattr(result, "rationale", ""),
            "emotion": getattr(result, "emotion", "neutral"),
            "action": getattr(result, "action", "NONE"),
            "reply": getattr(result, "reply", "..."),
            "skill_needed": skill_needed,
        }

    def _execute_vts_tools(self, emotion, action, vad=None):
        """Feedback Loop: Thực thi VTS tools và trả về lỗi nếu có (Agent-Zero logic)."""
        try:
            from vts_api import vts_bridge
            errors = []
            
            # 1. Update VAD parameters (Paralinguistics)
            if vad:
                vts_bridge.update_vad_params(*vad)
            
            # 2. Trigger Emotion Hotkey
            if emotion and emotion.lower() != "neutral":
                res = vts_bridge.trigger_emotion(emotion)
                if isinstance(res, dict) and res.get("status") == "error":
                    errors.append(f"VTS (Emotion) {res.get('reason')}")
            
            # 3. Trigger Action Hotkey
            if action and action.upper() != "NONE":
                res = vts_bridge.trigger_action(action)
                if isinstance(res, dict) and res.get("status") == "error":
                    errors.append(f"VTS (Action) {res.get('reason')}")
                    
            return ". ".join(errors) if errors else None
        except Exception as e:
            return f"Internal Tool Error: {str(e)}"

    def _call_and_parse_vbrain(
        self, api_messages, dynamic_temp, dynamic_max_tokens, prompt_args
    ):
        """
        Orchestrates the brain call using DSPy (Programming) instead of pure Prompting.
        """
        if not self.brain:
            # Fallback uses api_messages which already has string content
            content = self._call_model(
                api_messages, temperature=dynamic_temp, max_tokens=dynamic_max_tokens
            )
            if not content:
                return {"monologue": "", "emotion": "neutral", "action": "NONE", "reply": "..."}
            parsed = parse_vbrain_response(content)
        else:
            try:
                history_str = "\n".join([f"{m['role']}: {m['content']}" for m in api_messages[1:-1]])
                user_msg = api_messages[-1]["content"]

                start_brain = time.time()
                with dspy.context(lm=self.dspy_lm):
                    result = self._call_brain(prompt_args["structured"], history_str, user_msg)
                print(f"[Brain] Main generation took {time.time() - start_brain:.2f}s")
                parsed = self._parse_brain_result(result)

            except Exception as e:
                print(f"[Core] DSPy Brain Error: {e}. Falling back to standard LLM call.")
                content = self._call_model(
                    api_messages, temperature=dynamic_temp, max_tokens=dynamic_max_tokens
                )
                parsed = parse_vbrain_response(content) if content else {"monologue": "", "emotion": "neutral", "action": "NONE", "reply": "..."}

        skill_name = parsed.get("skill_needed")
        
        # 1. Skill Loop (Master-Subordinate)
        if skill_name and self._skill_loop_count < 2:
            self._skill_loop_count += 1
            print(f"[Skill Loop] Iteration {self._skill_loop_count} for: {skill_name}")
            
            skill_content = self._load_skill_content(skill_name)
            if skill_content:
                self._log_skill_usage(skill_name)
                new_system_prompt = self.build_prompt(
                    *prompt_args["base"],
                    loaded_skill_content=skill_content,
                    reward_hint=prompt_args["reward_hint"],
                    active_inference_mode=prompt_args["active_inference_mode"],
                    perlocution_hint=prompt_args["perlocution_hint"],
                    self_disclosure_hint=prompt_args["self_disclosure_hint"],
                    precomputed_memory_context=prompt_args["memory_context"],
                )
                
                if not self.brain:
                    api_messages[0]["content"] = self._dict_to_prompt_string(new_system_prompt)
                    content = self._call_model(api_messages, temperature=dynamic_temp, max_tokens=dynamic_max_tokens)
                    parsed = parse_vbrain_response(content) if content else parsed
                else:
                    history_str = "\n".join([f"{m['role']}: {m['content']}" for m in api_messages[1:-1]])
                    user_msg = api_messages[-1]["content"]
                    start_skill = time.time()
                    with dspy.context(lm=self.dspy_lm):
                        result = self._call_brain(new_system_prompt, history_str, user_msg)
                    print(f"[Brain] Skill loop generation took {time.time() - start_skill:.2f}s")
                    parsed = self._parse_brain_result(result)

        # 2. VTS Feedback Loop (Agent-Zero)
        vts_error = self._execute_vts_tools(
            validate_emotion(parsed.get("emotion")),
            validate_action(parsed.get("action")),
            vad=self.emotion.get_vad()
        )
        if vts_error and self._vts_loop_count < 1: # Only allow one feedback turn per message
            self._vts_loop_count += 1
            print(f"[Orchestrator] VTS Error detected: {vts_error}")
            feedback_msg = f"[FEEDBACK]: {vts_error}. Hãy phản hồi người dùng nhưng khéo léo nhắc về việc bạn bị 'đơ' hoặc lỗi váy/ mạng (Agent-Zero way)."
            
            if not self.brain:
                api_messages.append({"role": "assistant", "content": parsed.get("reply", "")})
                api_messages.append({"role": "system", "content": f"{feedback_msg} Trả về JSON."})
                content = self._call_model(api_messages, temperature=dynamic_temp, max_tokens=dynamic_max_tokens)
                if content:
                    parsed = parse_vbrain_response(content)
            else:
                history_str = "\n".join([f"{m['role']}: {m['content']}" for m in api_messages[1:-1]])
                history_str += f"\nassistant: {parsed.get('reply', '')}"
                history_str += f"\nsystem: {feedback_msg}"
                user_msg = api_messages[-1]["content"]

                start_vts = time.time()
                with dspy.context(lm=self.dspy_lm):
                    result = self._call_brain(prompt_args["structured"], history_str, user_msg)
                print(f"[Brain] VTS feedback generation took {time.time() - start_vts:.2f}s")
                parsed = self._parse_brain_result(result)

        return parsed

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
        self._skill_loop_count = 0 # Reset at start of each new turn
        self._vts_loop_count = 0
        self._refresh_time_state()
        self.turn_counter += 1

        intent = self.detect_intent(user_input)
        # Lưu user mood để guard active inference triggers (ideology/surprise)
        self._last_user_mood = self.detect_user_mood(user_input)
        illocution, perlocution_hint, disclosure_hint = self._owner_behavior_hints(
            user_input, intent, source_type
        )
        self.emotion.update(user_input, self.time_gap_hours, intent=intent)

        if source_type == "owner" and self.is_streaming:
            enqueue(PRIORITY_HIGH, maybe_refresh_from_emotion, self.emotion.get_state())
            # Sticky Context: Livestreaming (Plandex logic)
            self.memory.add_session_item("Em đang livestream và tâm sự với mọi người.", kind="context", is_sticky=True)

        self._track_stream_turn(user_input, source_type, viewer_data, stream_context)

        if self.turn_counter % REFLECTION_INTERVAL == 0:
            # Chỉ enqueue nếu không phải stream hoặc là chủ kênh (để tránh quá tải khi stream đông viewer)
            if not self.is_streaming or source_type == "owner":
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
            "structured": system_prompt,
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
                f"Diễn biến phiên chat (L2):\n{session_items}\n\n"
                f"Trạng thái cảm xúc: {emotion_state}\n\n"
                f"Nhiệm vụ: Tóm tắt 2-3 'Thấu hiểu quan trọng' về tình hình hiện tại (ví dụ: Người dùng đang bận, Người xem thích đùa nhây, Lyra đang mệt). "
                f"Trả về JSON cực ngắn: {{\"insights\": [\"thấu hiểu 1\", \"thấu hiểu 2\"]}}"
            )
            
            raw = self._call_light_model([
                {"role": "system", "content": "Bạn là subconscious của Lyra. Chỉ trả về JSON."},
                {"role": "user", "content": prompt}
            ], temperature=0.3, max_tokens=150, provider="gemini")
            
            if raw:
                match = re.search(r'\{.*\}', raw, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    insights = data.get("insights", [])
                    if insights:
                        from live_context import update_insights
                        update_insights(insights)
                        print(f"[Reflection] New insights: {insights}")
                        
                        # Tính năng tự động cập nhật kế hoạch stream đã bị vô hiệu hóa

        except Exception as e:
            print(f"[Core] Reflection loop error: {e}")

    def _update_plan_status(self, insights):
        """(Disabled) Trước đây dùng để cập nhật trạng thái các mục tiêu kế hoạch stream."""
        pass

    def _generate_stream_plan(self):
        """Generates dynamic goals for the session (Plandex/Letta-style)."""
        from config import STREAM_TITLE, STREAM_GOALS, STREAM_NOTES
        from live_context import update_plan
        
        print("[Core] Generating dynamic stream plan...")
        prompt = (
            f"Bạn là Lyra, Vtuber đang chuẩn bị stream.\n"
            f"Tiêu đề: {STREAM_TITLE}\n"
            f"Mục tiêu định sẵn: {', '.join(STREAM_GOALS)}\n"
            f"Ghi chú: {STREAM_NOTES}\n\n"
            f"Nhiệm vụ: Dựa trên thông tin trên, hãy liệt kê 3-4 mục tiêu cụ thể, vui vẻ cho buổi stream hôm nay. "
            f"Trả về JSON: {{\"plan\": [{{\"goal\": \"tên mục tiêu\", \"status\": \"pending\"}}]}}"
        )
        
        try:
            raw = self._call_light_model([
                {"role": "system", "content": "Chỉ trả về JSON."},
                {"role": "user", "content": prompt}
            ], temperature=0.7, provider="openrouter")
            
            if raw:
                match = re.search(r'\{.*\}', raw, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    plan = data.get("plan", [])
                    if plan:
                        update_plan(plan)
                        print(f"✓ Dynamic plan generated: {len(plan)} items")
        except Exception as e:
            print(f"[Core] Planning error: {e}")

    def get_proactive_message(self):
        """Generates a proactive message based on time and situation."""
        from prompts import PROACTIVE_TIME_TEMPLATES
        situation = "morning" if self.time_period == "morning" else "generic"
        template = random.choice(PROACTIVE_TIME_TEMPLATES.get(situation, ["..."]))
        
        messages = [
            {"role": "system", "content": "Bạn là Lyra, em gái 16 tuổi. Bạn luôn xưng 'em' và gọi 'anh'. Hãy nói lại câu sau một cách tự nhiên, đáng yêu nhất. Trả về văn bản thuần."},
            {"role": "user", "content": template}
        ]
        return self._call_light_model(messages, temperature=0.8, provider="openrouter")

    # [END OF MINIAI]
