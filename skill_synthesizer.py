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

class SkillSynthesizer:
    def __init__(self, skills_dir):
        self.skills_dir = skills_dir
        self.index_path = os.path.join(skills_dir, "_index.md")
        self.stats_path = os.path.join(skills_dir, "skill_stats.json")

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
            ])

            if not resp or resp.strip() == "{}":
                return None

            # Parse JSON result
            # Đôi khi model trả về text kèm JSON, cần dùng regex
            match = re.search(r"\{.*\}", resp, re.DOTALL)
            if not match:
                return None
            
            skill_data = json.loads(match.group())
            if not skill_data.get("skill_name") or not skill_data.get("content_md"):
                return None

            learned_name = self.save_skill(skill_data)
            
            # Sau khi học xong, tiện tay dọn dẹp các skill cũ luôn
            self.cleanup_stale_skills()
            
            return learned_name

        except Exception as e:
            print(f"[Synthesizer] Error: {e}")
            return None

    def save_skill(self, skill_data):
        name = skill_data["skill_name"]
        content = skill_data["content_md"]
        description = skill_data["description"]

        # 1. Lưu file .md
        file_path = os.path.join(self.skills_dir, f"{name}.md")
        if os.path.exists(file_path):
            # Nếu skill đã tồn tại, có thể merge hoặc skip
            # Ở đây ta skip để tránh loop vô tận
            return None

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        # 2. Cập nhật _index.md
        self.update_index(name, description)
        
        print(f"[Synthesizer] New skill learned: {name}")
        return name

    def update_index(self, name, description):
        if not os.path.exists(self.index_path):
            return
        
        with open(self.index_path, "a", encoding="utf-8") as f:
            f.write(f"| `{name}` | {description} |\n")

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
            # Bỏ qua các skill cốt lõi (built-in)
            if name in ["web_search", "memory_recall", "emotion_deep", "stream_manager"]:
                continue
            
            last_used = data.get("last_used", 0)
            call_count = data.get("call_count", 0)
            
            # Nếu skill đã tồn tại quá 30 ngày (dựa theo last_used hoặc created_at)
            # Ở đây ta dùng đơn giản: if (now - last_used) > 30 ngày AND call_count <= 2
            if (now - last_used) > thirty_days_sec and call_count <= 2:
                print(f"[Synthesizer] Forgetting stale skill: {name}")
                self._delete_skill(name)
                del stats[name]
                changed = True

        if changed:
            with open(self.stats_path, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
            self._rebuild_index(stats)

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
