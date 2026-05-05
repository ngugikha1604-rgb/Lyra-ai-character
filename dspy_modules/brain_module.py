import dspy
from .signatures import LyraChatSignature

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
