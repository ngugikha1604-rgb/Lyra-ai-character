import dspy

class LyraChatSignature(dspy.Signature):
    """You are Lyra, a 16-year-old VTuber sister. Be witty, smart, and technical."""
    system_context = dspy.InputField(desc="Core persona, rules, and live context")
    chat_history = dspy.InputField(desc="Recent chat logs")
    user_message = dspy.InputField(desc="Current message to reply to")
    
    # Outputs
    emotion = dspy.OutputField(desc="One of: neutral, happy, sad, angry, thinking...")
    action = dspy.OutputField(desc="VTube Studio action: WAVE, NOD, SHAKE_HEAD, THINK, NONE...")
    skill_needed = dspy.OutputField(desc="Name of skill to use (e.g., search_web), or 'NONE'")
    reply = dspy.OutputField(desc="The spoken response to the user")
