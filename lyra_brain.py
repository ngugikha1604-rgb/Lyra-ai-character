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
        "skill_needed": None,
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

    # Final fallback: text extraction if JSON fails completely
    # Try to find skill_needed via regex first
    skill_match = re.search(r'"skill_needed":\s*"(.*?)"', clean_content)
    if skill_match:
        default_res["skill_needed"] = skill_match.group(1)

    lines = clean_content.split('\n')
    cleaned_lines = []
    for line in lines:
        lower_line = line.lower()
        if '"monologue"' in lower_line or lower_line.startswith("monologue:"):
            continue
        if '"emotion"' in lower_line or lower_line.startswith("emotion:"):
            continue
        if '"action"' in lower_line or lower_line.startswith("action:"):
            continue
        if lower_line.strip() in ['{', '}', '```', '```json']:
            continue
        cleaned_lines.append(line)
    
    # Process each line to remove 'reply' tags
    final_lines = []
    for line in cleaned_lines:
        line_clean = re.sub(r'(?i)^"?reply"?\s*:\s*"?', '', line.strip())
        line_clean = line_clean.strip(' ",')
        if line_clean:
            final_lines.append(line_clean)

    fallback_text = ' '.join(final_lines).strip()
    
    default_res["reply"] = fallback_text or content

    return default_res





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
