import dspy
from .signatures import LyraChatSignature

class LyraBrain(dspy.Module):
    def __init__(self):
        super().__init__()
        self.generate = dspy.Predict(LyraChatSignature)

    def forward(self, persona, situation, memory, behavior_hints, chat_history, user_message):
        return self.generate(
            persona=persona,
            situation=situation,
            memory=memory,
            behavior_hints=behavior_hints,
            chat_history=chat_history,
            user_message=user_message,
        )
