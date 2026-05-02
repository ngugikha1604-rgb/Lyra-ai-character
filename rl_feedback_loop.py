import time
import json
import os
import threading
from datetime import datetime
from background_worker import enqueue, PRIORITY_NORMAL, PRIORITY_HIGH

RL_BUFFER_PATH = "rl_feedback_buffer.json"

class RLFeedbackLoop:
    """
    Reinforcement Learning from Human Feedback (RLHF) for Lyra's streaming interactions.
    Tracks Lyra's actions and viewer reactions to find "High Reward" response patterns.
    """
    def __init__(self, ai_instance):
        self.ai = ai_instance
        self.active_observations = [] # List of dicts: {action_time, reply, intent, reaction_buffer: []}
        self.lock = threading.Lock()
        self.reward_window = 15.0 # seconds
        self.buffer = []
        self._load_buffer()

    def _load_buffer(self):
        if os.path.exists(RL_BUFFER_PATH):
            try:
                with open(RL_BUFFER_PATH, "r", encoding="utf-8") as f:
                    self.buffer = json.load(f)
            except: self.buffer = []

    def _save_buffer(self):
        try:
            with open(RL_BUFFER_PATH, "w", encoding="utf-8") as f:
                json.dump(self.buffer[-200:], f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[RL] Save buffer error: {e}")

    def register_action(self, user_input, reply, intent, emotion):
        """Called when Lyra speaks. Starts a new reward observation window."""
        now = time.time()
        obs = {
            "action_time": now,
            "user_input": user_input,
            "reply": reply,
            "intent": intent,
            "emotion": emotion,
            "reaction_buffer": []
        }
        with self.lock:
            self.active_observations.append(obs)
        
        # Schedule evaluation after window closes
        threading.Timer(self.reward_window + 0.5, self._trigger_evaluation, args=[obs]).start()

    def ingest_viewer_message(self, message, sender_name):
        """Called for every incoming viewer message to build the reaction context."""
        now = time.time()
        with self.lock:
            # Clean up old observations (older than window + buffer)
            self.active_observations = [o for o in self.active_observations if now - o["action_time"] < self.reward_window + 5]
            
            for obs in self.active_observations:
                # If message arrived within 15s of Lyra speaking
                if 0 <= (now - obs["action_time"]) <= self.reward_window:
                    obs["reaction_buffer"].append(f"{sender_name}: {message}")

    def _trigger_evaluation(self, obs):
        """Moves observation to background worker for AI scoring."""
        if not obs["reaction_buffer"]:
            return
        enqueue(PRIORITY_NORMAL, self._evaluate_observation, obs)

    def _coerce_score(self, value):
        """Return a numeric reward score, clamped to the evaluator range."""
        try:
            score = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(-10.0, min(10.0, score))

    def _evaluate_observation(self, obs):
        """Uses light model (Qwen 0.5b) to score the reaction."""
        reaction_text = "\n".join(obs["reaction_buffer"][:15]) # Limit to 15 messages to stay within context
        
        prompt = (
            f"Bạn là giám khảo đánh giá mức độ thành công của Streamer AI Lyra.\n"
            f"Viewer hỏi: \"{obs.get('user_input', '')}\"\n"
            f"Lyra vừa nói: \"{obs['reply']}\"\n"
            f"Phản ứng của chat ngay sau đó:\n{reaction_text}\n\n"
            f"Hãy chấm điểm độ thành công (Reward) từ -10 đến +10.\n"
            f"- Điểm cộng (+): Chat cười (haha, hihi), khen (đỉnh, chất), dùng từ lóng của Lyra, chat rate tăng.\n"
            f"- Điểm trừ (-): Chat chửi, toxic, spam icon vô nghĩa, hoặc chat im lặng.\n"
            f"Trả về kết quả dưới dạng JSON: {{\"score\": <float>, \"reason\": \"<1 câu giải thích>\"}}"
        )

        try:
            # Use the AI's internal light model caller
            raw = self.ai._call_light_model([
                {"role": "system", "content": "Bạn là chuyên gia phân tích cảm xúc livestream. Chỉ trả về JSON."},
                {"role": "user", "content": prompt}
            ], temperature=0.1)
            
            if not raw: return
            
            # Extract JSON from potential markdown/extra text
            import re
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                eval_data = json.loads(match.group())
                obs["reward_score"] = self._coerce_score(eval_data.get("score", 0.0))
                obs["reason"] = eval_data.get("reason", "")
                # We don't need to keep the full reaction buffer in the permanent file
                obs["reaction_preview"] = reaction_text[:200]
                obs.pop("reaction_buffer", None)
                
                print(f"[RL] Scored: {obs['reward_score']} | Reply: {obs['reply'][:30]}...")
                
                if obs["reward_score"] >= 8.0:
                    enqueue(PRIORITY_NORMAL, self.ai.synthesizer.synthesize_from_rl, obs.get("user_input", ""), obs["reply"], obs["reaction_preview"], self.ai)
                
                with self.lock:
                    self.buffer.append(obs)
                    self._save_buffer()
        except Exception as e:
            print(f"[RL] Evaluation error: {e}")

    def consolidate_post_stream(self):
        """Post-stream Review Node: Analyze buffer, update Vector DB and persona instructions."""
        if not self.buffer: return
        
        print("[RL] Running Post-Stream Review Node...")
        # 1. Filter High Reward interactions (Score >= 7.0)
        # Sort by score descending
        sorted_buffer = sorted(self.buffer, key=lambda x: self._coerce_score(x.get("reward_score", 0)), reverse=True)
        high_reward = [obs for obs in sorted_buffer if self._coerce_score(obs.get("reward_score", 0)) >= 7.0]
        
        if not high_reward:
            print("[RL] No high reward interactions found today.")
            return

        # 2. Upsert to Pinecone for Few-Shot retrieval
        # Take up to 5 best distinct patterns
        for obs in high_reward[:5]:
            try:
                memory_text = f"Context: {obs['intent']} | Success Response: {obs['reply']}"
                # Add to episodic memory with specific metadata for RL
                self.ai.memory.add_item("rl_few_shot", memory_text, weight=2.0)
                print(f"[RL] Pattern promoted to Pinecone: {obs['reply'][:40]}")
            except Exception as e:
                print(f"[RL] Pinecone upsert error: {e}")

        # 3. Personality Evolution: Summarize successful vibe
        best_replies = "\n".join([f"- {o['reply']} (Reward: {o['reward_score']})" for o in high_reward[:8]])
        evolution_prompt = (
            f"Dưới đây là các câu trả lời đạt điểm cao nhất từ khán giả hôm nay:\n{best_replies}\n\n"
            f"Dựa trên các ví dụ này, hãy tóm tắt 1 chỉ dẫn ngắn gọn về phong cách nói chuyện (vibe) "
            f"mà Lyra nên phát huy để khán giả hài lòng hơn. Trả lời bằng tiếng Việt, cực ngắn (dưới 20 từ)."
        )
        
        try:
            vibe_instruction = self.ai._call_light_model([
                {"role": "system", "content": "Bạn là cố vấn cá tính cho AI Lyra."},
                {"role": "user", "content": evolution_prompt}
            ], temperature=0.7)
            
            if vibe_instruction:
                from live_context import add_constraint
                instruction = vibe_instruction.strip().replace('"', '')
                add_constraint(f"RL_FEEDBACK: {instruction}")
                print(f"[RL] Evolution Note: {instruction}")
        except Exception as e:
            print(f"[RL] Evolution error: {e}")

        # Clear buffer after successful consolidation to start fresh next session
        self.buffer = []
        self._save_buffer()
        print("[RL] Post-stream consolidation complete.")
