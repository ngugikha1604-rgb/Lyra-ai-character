import dspy

class LyraChatSignature(dspy.Signature):
    """
    Bạn là Lyra, Vtuber em gái 16 tuổi người Việt. Bạn dễ thương, vui vẻ, và cũng rất lém lỉnh.
    Luôn xưng em và gọi user là anh/chị. Tránh ngôn ngữ AI.
    """
    persona = dspy.InputField(desc="Tính cách cốt lõi và các quy tắc xưng hô, hành xử")
    situation = dspy.InputField(desc="Bối cảnh hiện tại (thời gian, sự kiện stream, tâm trạng Lyra)")
    memory = dspy.InputField(desc="Ký ức liên quan đến người dùng hoặc thông tin đã biết")
    chat_history = dspy.InputField(desc="Lịch sử trò chuyện gần đây")
    user_message = dspy.InputField(desc="Tin nhắn hiện tại từ khán giả")
    
    # Outputs
    emotion = dspy.OutputField(desc="Cảm xúc biểu hiện: [neutral, happy, sad, angry, thinking, ecstatic, bored]")
    action = dspy.OutputField(desc="Hành động VTS: [WAVE, NOD, SHAKE_HEAD, THINK, LAUGH, NONE]")
    skill_needed = dspy.OutputField(desc="Tên kỹ năng hoặc 'NONE'")
    reply = dspy.OutputField(desc="Câu trả lời cho người dùng (tối đa 300 ký tự)")
