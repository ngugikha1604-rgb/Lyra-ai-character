import os
import requests
import re
import sys

# 1. Update debug.py
with open("debug.py", "r", encoding="utf-8") as f:
    debug_content = f.read()

# Make it output attention and action
target_print = """            reply = result.get("reply", "[No reply]")
            original_reply = result.get("original_reply", "")
            monologue = result.get("monologue", "")

            if monologue:
                print(f"\\n[Monologue]: {monologue}")

            try:
                print(f"\\nLyra (VN): {reply}")
                if original_reply and original_reply != reply:
                    print(f"Lyra (EN): {original_reply}")
            except UnicodeEncodeError:
                print(
                    f"\\nLyra (encoded): {reply.encode('ascii', 'ignore').decode('ascii')}"
                )

            print("-" * 40)
            print(f"Emotion: {result.get('emotion')} | Intent: {result.get('intent')}")
            print(f"Mood: {result.get('mood')} | Affection: {result.get('affection')}")
            print(
                f"Time Gap: {result.get('time_gap_hours')} | Period: {result.get('time_period')}"
            )
            print("-" * 40)"""

replacement_print = """            reply = result.get("reply", "[No reply]")
            monologue = result.get("monologue", "")
            action = result.get("action", "NONE")
            
            # Since core.py uses attention as property
            attention = getattr(ai, "attention", result.get("attention", 10.0))

            if monologue:
                print(f"\\n>>> THINKING: {monologue}")

            try:
                print(f"\\nLyra: {reply}")
                if action and action != "NONE":
                    print(f"*(Action: {action})*")
            except UnicodeEncodeError:
                print(f"\\nLyra (encoded): {reply.encode('ascii', 'ignore').decode('ascii')}")

            print("-" * 40)
            print(f"Emotion: {result.get('emotion', 'neutral')} | Intent: {result.get('intent', 'none')} | Action: {action}")
            print(f"Mood: {result.get('mood', 0)} | Affection: {result.get('affection', 0)} | Attention: {attention:.1f}")
            print(f"Time Gap: {result.get('time_gap_hours', 0)}h | Period: {result.get('time_period', 'unknown')}")
            print("-" * 40)"""

if target_print in debug_content:
    debug_content = debug_content.replace(target_print, replacement_print)
    with open("debug.py", "w", encoding="utf-8") as f:
        f.write(debug_content)
    print("debug.py: UPDATED")
else:
    # Soft match
    pattern = re.compile(r'            reply = result\.get\("reply", "\[No reply\]"\).*?print\("-" \* 40\)', re.DOTALL)
    if pattern.search(debug_content):
        debug_content = pattern.sub(replacement_print, debug_content)
        with open("debug.py", "w", encoding="utf-8") as f:
            f.write(debug_content)
        print("debug.py: UPDATED VIA REGEX")
    else:
        print("debug.py: TARGET NOT FOUND")

# 2. Delete local memory files
has_deleted = False
for filename in ["memory.db", "memory.json", "models/memory.db"]:
    if os.path.exists(filename):
        try:
            os.remove(filename)
            print(f"Deleted {filename}")
            has_deleted = True
        except Exception as e:
            print(f"Could not delete {filename}: {e}")

# 3. Pinecone Index Flush
try:
    sys.path.append(os.getcwd())
    from config import PINECONE_API_KEY, PINECONE_INDEX
    if PINECONE_API_KEY:
        print(f"Flushing Pinecone Index: {PINECONE_INDEX}...")
        resp = requests.delete(
            f"https://api.pinecone.io/indexes/{PINECONE_INDEX}",
            headers={"Api-Key": PINECONE_API_KEY}
        )
        if resp.status_code in [200, 202]:
            print(f"Pinecone Index '{PINECONE_INDEX}' deleted successfully! It will be recreated on next run.")
        elif resp.status_code == 404:
            print(f"Pinecone Index '{PINECONE_INDEX}' does not exist, nothing to delete.")
        else:
            print(f"Pinecone delete returned: {resp.status_code} - {resp.text}")
    else:
        print("No Pinecone API Key configured, skipping Pinecone flush.")
except Exception as e:
    print(f"Could not check/flush Pinecone: {e}")

print("Memory Wipe Complete!")
