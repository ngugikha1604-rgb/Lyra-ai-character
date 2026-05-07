import os
import json
import re
from datetime import datetime, timedelta
from config import LIGHT_MODEL, LIGHT_BASE_URL
import requests
import time

SKILL_SYNTHESIZE_PROMPT = """You are a meta-learning assistant for Lyra, an AI character.
Analyze the following successful conversation snippets and identify a "Skill" or "Behavior Pattern" that Lyra demonstrated.

A "Skill" is a specific way of handling a type of request, a personality quirk, or a logical approach.

Return a JSON object:
{
  "skill_name": "snake_case_name",
  "description": "Short description for the index",
  "content_md": "Full markdown content with sections: # Skill Name, ### Instructions, ### Examples"
}

If no new unique skill is found, return {}."""


def safe_json_parse(text):
    """Parse JSON from model output, handling markdown code blocks and trailing text."""
    if not text:
        return {}
    text = re.sub(r'^```json\s*|\s*```$', '', text.strip())
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


class SkillSynthesizer:
    def __init__(self, skills_dir):
        self.skills_dir = skills_dir
        self.index_path = os.path.join(skills_dir, "_index.md")
        self.stats_path = os.path.join(skills_dir, "skill_stats.json")

    def _parse_skill_response(self, resp):
        """Extract and normalize a skill JSON object from model output."""
        if not resp or resp.strip() == "{}":
            return None

        match = re.search(r"\{.*\}", resp, re.DOTALL)
        if not match:
            return None

        skill_data = json.loads(match.group())
        if not isinstance(skill_data, dict):
            return None

        raw_name = str(skill_data.get("skill_name", "")).strip().lower()
        safe_name = re.sub(r"[^a-z0-9_]+", "_", raw_name).strip("_")
        content = str(skill_data.get("content_md", "")).strip()
        if not safe_name or not content:
            return None

        description = str(skill_data.get("description", "Kỹ năng tự học từ phản ứng tích cực của viewer")).strip()
        return {
            "skill_name": safe_name,
            "description": description or "Kỹ năng tự học từ phản ứng tích cực của viewer",
            "content_md": content,
        }

    def synthesize_from_rl(self, user_input, lyra_reply, reaction_text, core_engine):
        """
        Synthesizes a new behavioral skill based on high-reward stream interactions.
        """
        try:
            print("[Synthesizer] Analyzing high-reward interaction for new skill...")
            prompt = (
                f"Viewer asked: \"{user_input}\"\n"
                f"Lyra responded: \"{lyra_reply}\"\n"
                f"Audience reaction (very positive): \"{reaction_text}\"\n\n"
                f"What specific conversational 'Skill' or 'Behavior Pattern' did Lyra use here to get such a positive reaction? "
                f"Extract this as a repeatable skill."
            )
            
            resp = core_engine._call_light_model([
                {"role": "system", "content": SKILL_SYNTHESIZE_PROMPT},
                {"role": "user", "content": prompt}
            ], provider="gemini")

            skill_data = self._parse_skill_response(resp)
            if not skill_data:
                return None

            learned_name = self.save_skill(skill_data)
            self.cleanup_stale_skills()
            return learned_name
        except Exception as e:
            print(f"[Synthesizer] RL Synthesize Error: {e}")
            return None


    def synthesize(self, conversation_history, core_engine):
        """
        Gần giống như memory extraction nhưng tập trung vào 'cách cư xử' hoặc 'kỹ năng xử lý'
        """
        # Giới hạn phân tích 10 lượt chat gần nhất
        snippet = conversation_history[-10:]
        snippet_text = ""
        for msg in snippet:
            role = "Anh" if msg["role"] == "user" else "Lyra"
            snippet_text += f"{role}: {msg['content']}\n"

        try:
            # Sử dụng light model để tiết kiệm
            resp = core_engine._call_light_model([
                {"role": "system", "content": SKILL_SYNTHESIZE_PROMPT},
                {"role": "user", "content": f"Analyze this conversation for new skills:\n\n{snippet_text}"}
            ], provider="gemini")

            skill_data = self._parse_skill_response(resp)
            if not skill_data:
                return None

            learned_name = self.save_skill(skill_data)
            
            # Sau khi học xong, tiện tay dọn dẹp các skill cũ luôn
            self.cleanup_stale_skills()
            
            return learned_name

        except Exception as e:
            print(f"[Synthesizer] Error: {e}")
            return None

    def save_skill(self, skill_data, overwrite=False):
        name = skill_data["skill_name"]
        content = skill_data["content_md"]
        description = skill_data.get("description", "Kỹ năng tự học")
        os.makedirs(self.skills_dir, exist_ok=True)

        # 1. Lưu file .md
        file_path = os.path.join(self.skills_dir, f"{name}.md")
        if os.path.exists(file_path):
            # Nếu skill đã tồn tại và overwrite=False, skip để tránh loop vô tận
            if not overwrite:
                return None
            # Nếu overwrite=True, xóa file cũ để ghi đè
            self._delete_skill(name)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        # 2. Cập nhật _index.md
        self.update_index(name, description)
        self._register_skill_stats(name, description)
        
        print(f"[Synthesizer] New skill learned: {name}")
        return name

    def update_index(self, name, description):
        if not os.path.exists(self.index_path):
            return
        
        with open(self.index_path, "a", encoding="utf-8") as f:
            f.write(f"| `{name}` | {description} |\n")

    def _register_skill_stats(self, name, description):
        stats = {}
        if os.path.exists(self.stats_path):
            try:
                with open(self.stats_path, "r", encoding="utf-8") as f:
                    stats = json.load(f)
            except Exception:
                stats = {}

        now = time.time()
        stats[name] = {
            **stats.get(name, {}),
            "description": description,
            "call_count": stats.get(name, {}).get("call_count", 0),
            "last_used": stats.get(name, {}).get("last_used", now),
            "created_at": stats.get(name, {}).get("created_at", now),
        }
        with open(self.stats_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    def cleanup_stale_skills(self):
        """
        Xóa các skill không được gọi quá 2 lần trong vòng 30 ngày.
        """
        if not os.path.exists(self.stats_path):
            return

        try:
            with open(self.stats_path, "r", encoding="utf-8") as f:
                stats = json.load(f)
        except Exception:
            return

        now = time.time()
        thirty_days_sec = 30 * 24 * 60 * 60
        changed = False

        for name, data in list(stats.items()):
            # Bỏ qua các skill cốt lõi (built-in) hoặc được đánh dấu protected
            if name in ["web_search", "memory_recall", "emotion_deep", "stream_manager"]:
                continue
            if data.get("protected", False):
                continue
            
            last_used = data.get("last_used", 0)
            call_count = data.get("call_count", 0)
            created_at = data.get("created_at", last_used)
            age_days = (now - created_at) / thirty_days_sec * 30
            
            # Chỉ xóa skill nếu: quá 30 ngày tuổi VÀ ít được dùng (≤2 lần)
            # Hoặc: rất ít dùng (0 lần) VÀ trên 60 ngày tuổi
            if (age_days > 30 and call_count <= 2) or (call_count == 0 and age_days > 60):
                print(f"[Synthesizer] Forgetting stale skill: {name}")
                self._delete_skill(name)
                del stats[name]
                changed = True

        if changed:
            with open(self.stats_path, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
            self._rebuild_index(stats)

    def get_skill_context(self, skill_name):
        md_path = os.path.join(self.skills_dir, f"{skill_name}.md")
        if os.path.exists(md_path):
            try:
                with open(md_path, "r", encoding="utf-8") as f:
                    return f.read()
            except: pass
        return ""

    def _delete_skill(self, name):
        md_path = os.path.join(self.skills_dir, f"{name}.md")
        if os.path.exists(md_path):
            os.remove(md_path)

    def _rebuild_index(self, stats):
        """Xây dựng lại file _index.md dựa trên stats hiện tại"""
        header = """# Lyra Skill Index

Chào Lyra! Đây là danh sách các kỹ năng bổ sung mà bạn có thể yêu cầu khi cần giải quyết các vấn đề phức tạp. Để sử dụng, hãy điền tên skill vào trường `skill_needed` trong phản hồi JSON của bạn.

| Skill Name | Description |
|------------|-------------|
| `memory_recall` | Sử dụng khi bạn cần truy xuất sâu vào ký ức cũ, các sự kiện quan trọng hoặc chi tiết cụ thể về người dùng. |
| `web_search` | Sử dụng khi người dùng hỏi về kiến thức mới, tin tức, hoặc những thứ bạn không biết chắc chắn. |
| `emotion_deep` | Sử dụng khi cuộc đối thoại trở nên nghiêm túc hoặc nhạy cảm, cần phân tích tâm lý sâu hơn. |
| `stream_manager` | Các quy tắc nâng cao về việc điều phối viewer và sự kiện trên livestream. |
"""
        with open(self.index_path, "w", encoding="utf-8") as f:
            f.write(header)
            for name, data in stats.items():
                if name not in ["web_search", "memory_recall", "emotion_deep", "stream_manager"]:
                    desc = data.get("description", "Kỹ năng tự học")
                    f.write(f"| `{name}` | {desc} |\n")
