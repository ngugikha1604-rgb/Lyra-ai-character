import sys
import requests
from core import MiniAI
from config import BASE_URL, USE_OLLAMA, DEFAULT_MODEL


def main():
    print("========================================")
    print("Lyra AI - Terminal Debug Mode")
    print("Mode:", "OLLAMA" if USE_OLLAMA else "GROQ")
    print("Model:", DEFAULT_MODEL)
    print("Type 'exit' or 'quit' to stop.")
    print("========================================\n")

    # 1. Connection Check
    if USE_OLLAMA:
        print(f"[Check] Verifying Ollama at {BASE_URL}...")
        try:
            tags_url = BASE_URL.replace("/api/chat", "/api/tags")
            res = requests.get(tags_url, timeout=5)
            if res.status_code == 200:
                print("✓ Ollama is online.")
            else:
                print(
                    f"! Ollama returned status {res.status_code}. Check if it is running correctly."
                )
        except Exception as e:
            print(f"! Could not connect to Ollama: {e}")
            print("  Make sure Ollama is started and reachable at localhost:11434")

    try:
        # 2. Khởi tạo AI engine
        print("[System] Loading Lyra AI...")
        ai = MiniAI()

        print(f"[System] AI Loaded. Current Mood: {ai.mood}, Affection: {ai.affection}")
        if ai.should_greet:
            print("[System] Note: Time-based greeting triggered upon startup.")

    except Exception as e:
        print(f"[ERROR] Failed to initialize MiniAI: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    # 3. Chat Loop
    while True:
        try:
            user_input = input("\nYou: ")

            if user_input.lower() in ("exit", "quit"):
                print("Exiting debug mode...")
                break

            if not user_input.strip():
                continue

            print("Lyra is thinking...")

            # Gọi hàm chat của MiniAI
            result = ai.chat(user_input)

            reply = result.get("reply", "[No reply]")
            original_reply = result.get("original_reply", "")
            monologue = result.get("monologue", "")

            if monologue:
                print(f"\n[Monologue]: {monologue}")

            try:
                print(f"\nLyra (VN): {reply}")
                if original_reply and original_reply != reply:
                    print(f"Lyra (EN): {original_reply}")
            except UnicodeEncodeError:
                print(
                    f"\nLyra (encoded): {reply.encode('ascii', 'ignore').decode('ascii')}"
                )

            print("-" * 40)
            print(f"Emotion: {result.get('emotion')} | Intent: {result.get('intent')}")
            print(f"Mood: {result.get('mood')} | Affection: {result.get('affection')}")
            print(
                f"Time Gap: {result.get('time_gap_hours')} | Period: {result.get('time_period')}"
            )
            print("-" * 40)

        except KeyboardInterrupt:
            print("\nExiting debug mode...")
            break
        except Exception as e:
            print(f"\n[ERROR] Exception during chat: {e}")
            import traceback

            traceback.print_exc()


if __name__ == "__main__":
    main()
