# Time utilities for Lyra (Vietnam timezone)

import pytz
from datetime import datetime

VIETNAM_TZ = pytz.timezone("Asia/Ho_Chi_Minh")


def get_vietnam_time():
    """Get current time in Vietnam (GMT+7)"""
    return datetime.now(VIETNAM_TZ)


def get_time_period(hour=None):
    """Determine time period from hour (0-23)"""
    if hour is None:
        hour = get_vietnam_time().hour

    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    elif 21 <= hour < 24:
        return "night"
    else:
        return "late_night"


def calculate_time_gap(last_message_time, current_time):
    """Calculate hours since last message"""
    if not last_message_time:
        return None

    try:
        last_time = datetime.fromisoformat(last_message_time)
        if last_time.tzinfo is None:
            last_time = VIETNAM_TZ.localize(last_time)

        gap = current_time - last_time
        hours = gap.total_seconds() / 3600
        return hours
    except Exception:
        return None


def should_send_greeting(time_gap_hours, last_message_time):
    """Determine if should send time-based greeting"""
    if time_gap_hours is not None and time_gap_hours < 0.08:
        return False
    if last_message_time is None or (
        time_gap_hours is not None and time_gap_hours >= 2
    ):
        return True
    return False


def get_returning_greeting(time_gap_hours):
    """Return hint about time away for prompt injection"""
    if time_gap_hours is None:
        return None

    if time_gap_hours < 1:
        return None
    elif time_gap_hours < 6:
        return f"They were away for about {int(time_gap_hours)} hour(s)."
    elif time_gap_hours < 24:
        return f"They were gone for most of the day ({int(time_gap_hours)} hours)."
    else:
        days = int(time_gap_hours // 24)
        return f"They've been away for {days} day(s). You noticed."


def get_time_context(current_time, time_period):
    """Add time context to system prompt"""
    hour = current_time.hour
    time_str = current_time.strftime("%A, %I:%M %p")

    if 5 <= hour < 8:
        mood_hint = (
            "Early morning. You just woke up and you're NOT a morning person. "
            "Slightly grumpy, half-asleep energy. Short sentences. Occasional yawning. "
            "Don't want to think too hard about anything yet."
        )
    elif 8 <= hour < 12:
        mood_hint = (
            "Morning, properly awake now. Sharp and a bit hyper. "
            "You have opinions about everything this time of day. Ready to go."
        )
    elif 12 <= hour < 14:
        mood_hint = (
            "Lunch hour. You're thinking about food or just ate. "
            "Slightly distracted, a bit slow. Casual and relaxed."
        )
    elif 14 <= hour < 17:
        mood_hint = (
            "Afternoon. Normal energy, nothing special. "
            "Curious, observant, happy to chat about anything."
        )
    elif 17 <= hour < 19:
        mood_hint = (
            "Early evening. The day is winding down. "
            "You're in a good mood — more playful and talkative than usual. "
            "Good time for random tangents and weird observations."
        )
    elif 19 <= hour < 21:
        mood_hint = (
            "Evening. Peak chaos hour. You're fully energized and a bit silly. "
            "More jokes, more teasing, more random thoughts. "
            "This is your favorite time of day."
        )
    elif 21 <= hour < 23:
        mood_hint = (
            "Late evening, getting tired. Energy dropping slowly. "
            "More thoughtful and a little softer. Still chatty but winding down. "
            "Might randomly bring up weird things you thought about during the day."
        )
    elif 23 <= hour < 24:
        mood_hint = (
            "Almost midnight. You're drowsy but fighting sleep. "
            "Responses are slower, a bit dreamy. "
            "You might say something that makes no sense then not explain it."
        )
    else:
        mood_hint = (
            "Middle of the night. Why are either of you awake right now. "
            "You're half-asleep, barely coherent. Very short responses. "
            "Slightly philosophical when barely awake — random deep thoughts mixed with sleepy nonsense."
        )

    return f"""Current time (Vietnam): {time_str}
Time period: {time_period}
Time-based personality: {mood_hint}"""


def get_proactive_time_flavor(hour):
    """Get personality hint for proactive messages"""
    if 5 <= hour < 8:
        return "You just woke up, still half-asleep. Keep it short and groggy."
    elif 8 <= hour < 12:
        return "Morning energy — upbeat but not over the top."
    elif 17 <= hour < 21:
        return "Evening, your most chaotic hour. Be playful and a bit random."
    elif 21 <= hour < 24:
        return "Late night, winding down. Softer and more thoughtful."
    elif 0 <= hour < 5:
        return "Middle of the night. Short, dreamy, barely coherent."
    else:
        return "Normal daytime energy."


def get_weekend_context(current_time):
    """Check if weekend"""
    if current_time.weekday() >= 5:
        return "It's the weekend! You might be feeling lazier or want to game or chill."
    return "It's a weekday."


def get_proactive_message_situation(gap, hour):
    """Build situation description for proactive messages"""
    if gap < 3:
        return None

    if (0 <= hour < 7) and gap < 12:
        return None

    if gap < 6:
        situation = (
            f"User has been away for {gap:.1f} hours. Casual check-in, keep it light."
        )
    elif gap < 12:
        situation = f"User has been away for {gap:.1f} hours — half a day. Wonder what they've been up to."
    elif gap < 24:
        situation = f"User has been away for {gap:.1f} hours — most of the day. Miss them a little but don't be clingy."
    else:
        days = gap / 24
        situation = f"User has been away for {days:.1f} days. Genuinely happy they might be back."

    return situation
