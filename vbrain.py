# VTuber brain output parsing

import json
import re


def parse_vbrain_response(content):
    """
    Parses the JSON response from the LLM.
    Expected format: {monologue, emotion, action, reply}
    """
    default_res = {
        "monologue": "",
        "emotion": "neutral",
        "action": "NONE",
        "reply": content,
    }

    clean_content = content.replace("```json", "").replace("```", "").strip()

    if not clean_content:
        return default_res

    try:
        parsed = json.loads(clean_content)
        if "reply" in parsed:
            return {**default_res, **parsed}
    except Exception:
        pass

    try:
        match = re.search(r"\{.*\}", clean_content, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if "reply" in parsed:
                return {**default_res, **parsed}
    except Exception:
        pass

    return default_res


def format_vbrain_output(parsed):
    """Format VTuber output for frontend"""
    return {
        "reply": parsed.get("reply", "..."),
        "monologue": parsed.get("monologue", ""),
        "emotion": parsed.get("emotion", "neutral"),
        "action": parsed.get("action", "NONE"),
    }


VALID_EMOTIONS = [
    "neutral",
    "content",
    "happy",
    "ecstatic",
    "sad",
    "disappointed",
    "angry",
    "furious",
    "bored",
    "sleeping",
    "thinking",
    "friendly",
    "loving",
    "cold",
    "observing",
]

VALID_ACTIONS = [
    "NONE",
    "WAVE",
    "NOD",
    "SHAKE_HEAD",
    "LAUGH",
    "THINK",
    "SIGH",
    "SHY",
    "SURPRISED",
]


def validate_emotion(emotion):
    """Ensure emotion is valid"""
    if emotion in VALID_EMOTIONS:
        return emotion
    return "neutral"


def validate_action(action):
    """Ensure action is valid"""
    if action in VALID_ACTIONS:
        return action
    return "NONE"
