# live_context.py — Letta-style hot state manager
# Manages a JSON file containing the current "live context" that is injected
# directly into the system prompt every turn. This is ephemeral, short-lived state
# that changes rapidly during streams (donations, viewer arrivals, current focus, etc.)

import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_CONTEXT_PATH = os.path.join(BASE_DIR, "live_context.json")

# Default TTL for the entire context block (minutes)
DEFAULT_CONTEXT_TTL_MINUTES = 20

# TTL for specific transient fields (minutes)
TRANSIENT_FIELD_TTL = {
    "latest_event": 5,
    "priority_mentions": 10,
    "chat_vibe": 10,
    "energy_label": 15,
    "current_insights": 15,
    "stream_plan": 60,
}

# Lock for thread-safe file operations
_context_lock = threading.Lock()


def _now_iso() -> str:
    """Current UTC time in ISO format with timezone"""
    return datetime.now(timezone.utc).isoformat()


def load_live_context() -> Dict[str, Any]:
    """
    Load live_context.json from disk. If file doesn't exist or is corrupted,
    return a fresh default context.
    """
    with _context_lock:
        if not os.path.exists(LIVE_CONTEXT_PATH):
            return get_default_context()

        try:
            with open(LIVE_CONTEXT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Ensure all required fields exist
            for k, v in get_default_context().items():
                data.setdefault(k, v)
            return data
        except (json.JSONDecodeError, OSError) as e:
            print(f"[LiveContext] Load error: {e}. Using default.")
            return get_default_context()


def save_live_context(data: Dict[str, Any]) -> None:
    """Write live_context.json to disk."""
    with _context_lock:
        try:
            with open(LIVE_CONTEXT_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as e:
            print(f"[LiveContext] Save error: {e}")


def get_default_context() -> Dict[str, Any]:
    """Return a fresh, neutral live context."""
    return {
        "stream_active": False,
        "updated_at": _now_iso(),
        "mood_label": "",
        "energy_label": "",
        "current_focus": "",
        "latest_event": "",
        "chat_vibe": "",
        "priority_mentions": [],
        "constraints": [],
        "current_insights": [],
        "stream_plan": [],
        "expires_after_minutes": DEFAULT_CONTEXT_TTL_MINUTES,
    }


def update_field(key: str, value: Any, ttl_minutes: Optional[int] = None) -> None:
    """
    Update a single field in live_context.json and reset updated_at.
    For transient fields, respects their specific TTL.
    """
    data = load_live_context()

    # Check if this field has expired (if value should be cleared)
    if key in TRANSIENT_FIELD_TTL and value in ("", None):
        # If clearing a field, that's fine
        pass
    elif key in TRANSIENT_FIELD_TTL:
        # Field has a TTL — we'll let the consumer check expiry, not here
        pass

    data[key] = value
    data["updated_at"] = _now_iso()
    save_live_context(data)


def update_multiple(updates: Dict[str, Any]) -> None:
    """Update multiple fields atomically."""
    data = load_live_context()
    data.update(updates)
    data["updated_at"] = _now_iso()
    save_live_context(data)


def reset_live_context() -> None:
    """Reset to neutral/inactive state (called after stream stop)."""
    save_live_context(get_default_context())


def is_stale(max_age_minutes: int = DEFAULT_CONTEXT_TTL_MINUTES) -> bool:
    """Check if the context has expired based on updated_at."""
    data = load_live_context()
    updated_str = data.get("updated_at", "")
    if not updated_str:
        return True
    try:
        updated = datetime.fromisoformat(updated_str)
        now = datetime.now(timezone.utc)
        age = (now - updated).total_seconds() / 60.0
        return age > max_age_minutes
    except Exception:
        return True


def maybe_refresh_from_emotion(emotion_state: Dict[str, Any]) -> None:
    """
    Periodically update mood_label and energy_label from the current EmotionEngine state.
    Called from core.py after each emotion update (owner turns only, to avoid noise).
    """
    mood = emotion_state.get("mood", 0)
    attention = emotion_state.get("attention", 5)
    # Simple mapping
    mood_label = (
        "playful"
        if mood > 6
        else "happy"
        if mood > 3
        else "grumpy"
        if mood < -6
        else "sad"
        if mood < -3
        else "neutral"
    )
    energy_label = "high" if attention >= 7 else "low" if attention <= 3 else "medium"
    update_multiple(
        {
            "mood_label": mood_label,
            "energy_label": energy_label,
        }
    )


def get_live_context_block(max_lines: int = 6) -> str:
    """
    Format the current live context as a concise prompt block.
    Returns empty string if context is stale or inactive.
    """
    if is_stale():
        # Optionally reset on read if stale? For now just return empty.
        return ""

    data = load_live_context()
    if not data.get("stream_active", False):
        return ""

    lines = ["[LIVE_CONTEXT]"]

    # Current focus — always show if present
    focus = data.get("current_focus", "")
    if focus:
        lines.append(f"Current activity: {focus}")

    # Latest event (transient)
    event = data.get("latest_event", "")
    if event:
        lines.append(f"Recent event: {event}")

    # Priority mentions — only the most recent 2-3
    mentions = data.get("priority_mentions", [])
    if mentions:
        # Keep only last 3
        for m in mentions[-3:]:
            if m:
                lines.append(f"Note: {m}")

    # Chat vibe
    vibe = data.get("chat_vibe", "")
    if vibe:
        lines.append(f"Chat vibe: {vibe}")

    # Mood/energy (optional, can be suppressed if too noisy)
    mood_lbl = data.get("mood_label", "")
    energy_lbl = data.get("energy_label", "")
    if mood_lbl and energy_lbl:
        lines.append(f"Lyra's state: {mood_lbl}, {energy_lbl}")

    # Constraints
    constraints = data.get("constraints", [])
    if constraints:
        lines.append(f"Constraints: {'; '.join(constraints)}")

    # Current insights (from Reflection Loop)
    insights = data.get("current_insights", [])
    if insights:
        lines.append("[INSIGHTS]")
        for insight in insights[:3]:
            lines.append(f"• {insight}")
    
    # Stream plan (from Dynamic Planning)
    plan = data.get("stream_plan", [])
    if plan:
        lines.append("[STREAM PLAN]")
        pending = [p for p in plan if p.get("status") == "pending"]
        for p in pending[:3]:
            lines.append(f"□ {p['goal']}")

    lines.append("[/LIVE_CONTEXT]")
    block = "\n".join(lines[:max_lines + 4]) # Allow a bit more for new sections
    return block


# Convenience wrappers for common updates


def set_stream_active(active: bool, focus: str = "") -> None:
    """Called on stream start/stop."""
    update_multiple(
        {
            "stream_active": active,
            "current_focus": focus if active else "",
            "chat_vibe": "",
            "latest_event": "",
            "priority_mentions": [],
            "mood_label": "",
            "energy_label": "",
        }
    )


def record_donation(viewer_name: str, amount: str) -> None:
    """Call when a donor sends a message or Super Chat arrives."""
    update_field("latest_event", f"{viewer_name} donated {amount}", ttl_minutes=5)
    # Add to priority mentions
    data = load_live_context()
    mentions = data.get("priority_mentions", [])
    mentions.append(f"Thank {viewer_name} for {amount} donation")
    # Keep last 5
    update_field("priority_mentions", mentions[-5:], ttl_minutes=10)
    update_field("energy_label", "high", ttl_minutes=15)


def record_regular_arrival(
    viewer_name: str, total_streams: int, affection: int
) -> None:
    """Call when a returning regular viewer sends their first message of the stream."""
    data = load_live_context()
    mentions = data.get("priority_mentions", [])
    # Friendly note — short TTL
    mentions.append(f"{viewer_name} (regular) is here")
    update_field("priority_mentions", mentions[-5:], ttl_minutes=10)


def record_milestone(description: str) -> None:
    """Call when a stream milestone is achieved."""
    update_field("latest_event", f"Milestone: {description}", ttl_minutes=10)
    data = load_live_context()
    mentions = data.get("priority_mentions", [])
    mentions.append(f"Celebrated: {description}")
    update_field("priority_mentions", mentions[-5:], ttl_minutes=15)


def update_chat_vibe(vibe: str) -> None:
    """Call periodically from ChatPatternAnalyzer."""
    update_field("chat_vibe", vibe, ttl_minutes=10)


def add_constraint(constraint: str) -> None:
    """Add a temporary constraint (e.g. 'no spoilers')."""
    data = load_live_context()
    constraints = data.get("constraints", [])
    if constraint not in constraints:
        constraints.append(constraint)
        update_field("constraints", constraints, ttl_minutes=30)


def clear_constraint(constraint: str) -> None:
    """Remove a constraint when it's no longer needed."""
    data = load_live_context()
    constraints = data.get("constraints", [])
    if constraint in constraints:
        constraints.remove(constraint)
        update_field("constraints", constraints)

def update_insights(insights: list[str]) -> None:
    """Update current mid-session insights."""
    update_field("current_insights", insights, ttl_minutes=15)

def update_plan(plan_items: list[dict]) -> None:
    """Update dynamic stream plan."""
    update_field("stream_plan", plan_items, ttl_minutes=60)
