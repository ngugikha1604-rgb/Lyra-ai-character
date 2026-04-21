import sys
import requests
from core import MiniAI
from config import BASE_URL, USE_OLLAMA, DEFAULT_MODEL


def main():
    print("========================================")
    print("Lyra AI - Terminal Debug Mode")
    print("Type 'exit' or 'quit' to stop.")
    print("========================================\n")

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
            monologue = result.get("monologue", "")
            action = result.get("action", "NONE")
            
            # Since core.py uses attention as property
            attention = getattr(ai, "attention", result.get("attention", 10.0))

            if monologue:
                print(f"\n>>> THINKING: {monologue}")

            try:
                print(f"\nLyra: {reply}")
                if action and action != "NONE":
                    print(f"*(Action: {action})*")
            except UnicodeEncodeError:
                print(f"\nLyra (encoded): {reply.encode('ascii', 'ignore').decode('ascii')}")

            print("-" * 40)
            print(f"Emotion: {result.get('emotion', 'neutral')} | Intent: {result.get('intent', 'none')} | Action: {action}")
            print(f"Mood: {result.get('mood', 0)} | Affection: {result.get('affection', 0)} | Attention: {attention:.1f}")
            print(f"Time Gap: {result.get('time_gap_hours', 0)}h | Period: {result.get('time_period', 'unknown')}")
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
