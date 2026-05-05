import dspy
from dspy.teleprompt import BootstrapFewShot

# ==========================================
# 1. Định nghĩa Signature (Input / Output)
# ==========================================
class LyraChatSignature(dspy.Signature):
    """You are Lyra, a 16-year-old VTuber sister. Be witty, smart, and technical."""
    system_context = dspy.InputField(desc="Core persona, rules, and live context")
    chat_history = dspy.InputField(desc="Recent chat logs")
    user_message = dspy.InputField(desc="Current message to reply to")
    
    # DSPy sẽ tự sinh ra trường 'rationale' nếu dùng ChainOfThought
    emotion = dspy.OutputField(desc="One of: neutral, happy, sad, angry, thinking...", prefix="Emotion:")
    action = dspy.OutputField(desc="VTube Studio action: WAVE, NOD, SHAKE_HEAD, THINK, NONE...", prefix="Action:")
    skill_needed = dspy.OutputField(desc="Name of skill to use (e.g., search_web), or 'NONE'", prefix="Skill:")
    reply = dspy.OutputField(desc="The spoken response to the user", prefix="Reply:")

# ==========================================
# 2. Định nghĩa Module (Brain)
# ==========================================
class LyraBrain(dspy.Module):
    def __init__(self):
        super().__init__()
        # Sử dụng ChainOfThought để tự động có phần suy luận (monologue)
        self.generate = dspy.ChainOfThought(LyraChatSignature)
    
    def forward(self, system_context, chat_history, user_message):
        return self.generate(
            system_context=system_context, 
            chat_history=chat_history, 
            user_message=user_message
        )

# ==========================================
# 3. Kịch bản Compile
# ==========================================
def main():
    print("Khoi tao DSPy...")
    
    # Thiết lập LM: Ở đây giả định dùng Ollama chạy local với model llama3
    # Nếu bạn dùng mô hình khác (như qwen hay phi3), hãy đổi tên model
    lm = dspy.LM('ollama_chat/qwen0.5b', api_base="http://localhost:11434")
    dspy.configure(lm=lm)
    
    print("Chuan bi Training Set (Du lieu mau)...")
    # Đây là các "Gold Standard" mẫu. Càng nhiều mẫu chuẩn, Lyra càng giống tính cách.
    trainset = [
        dspy.Example(
            system_context="Lyra is streaming. Mood is happy.",
            chat_history="User: Hello Lyra!",
            user_message="Anh mới đi làm về đây.",
            emotion="happy",
            action="WAVE",
            skill_needed="NONE",
            reply="A, anh đi làm về rồi à! Hôm nay trên công ty có gì vui không kể em nghe với!"
        ).with_inputs('system_context', 'chat_history', 'user_message'),
        
        dspy.Example(
            system_context="Lyra is streaming. Mood is neutral. User is asking a coding question.",
            chat_history="User: I'm stuck on a bug.",
            user_message="Em biết cách fix lỗi CORS trong Python không?",
            emotion="thinking",
            action="THINK",
            skill_needed="NONE",
            reply="Lỗi CORS à? Khó chịu phết nhỉ! Thường thì anh phải cấu hình server cho phép origin từ frontend. Anh đang dùng Flask hay FastAPI thế?"
        ).with_inputs('system_context', 'chat_history', 'user_message'),
    ]
    
    print("Thiet lap Optimizer (BootstrapFewShot)...")
    # Hàm đánh giá đơn giản: kiểm tra xem AI có sinh ra reply không và emotion có hợp lệ không
    def simple_metric(gold, pred, trace=None):
        valid_emotions = ["neutral", "happy", "sad", "angry", "thinking"]
        return len(pred.reply) > 0 and pred.emotion in valid_emotions

    # Tối ưu hóa: dạy AI từ các ví dụ trên
    optimizer = BootstrapFewShot(metric=simple_metric, max_bootstrapped_demos=2, max_labeled_demos=2)
    
    print("Dang tien hanh Compile... (Viec nay can Ollama dang chay tren localhost:11434)")
    try:
        # Bắt đầu compile
        compiled_lyra = optimizer.compile(LyraBrain(), trainset=trainset)
        
        # Lưu kết quả ra file JSON
        print("Luu model da compile ra file 'lyra_compiled_v1.json'...")
        compiled_lyra.save("lyra_compiled_v1.json")
        print("Thanh cong! Da tao file JSON.")
        
    except Exception as e:
        print(f"Loi khi Compile: {e}")
        print("Luu y: Hay chac chan rang ban da bat Ollama va da pull model 'llama3' (hoac sua ten model trong code cho khop).")

if __name__ == "__main__":
    main()
