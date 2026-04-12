import sys
from core import MiniAI

def main():
    print("========================================")
    print("🔮 Lyra AI - Terminal Debug Mode 🔮")
    print("Type 'exit' or 'quit' to stop.")
    print("========================================\n")

    try:
        # Khởi tạo AI engine
        ai = MiniAI()
        
        print(f"[System] AI Loaded. Current Mood: {ai.mood}, Affection: {ai.affection}")
        if ai.should_greet:
            print("[System] Note: Time-based greeting triggered upon startup.")
            
    except Exception as e:
        print(f"[ERROR] Failed to initialize MiniAI: {e}")
        sys.exit(1)

    while True:
        try:
            user_input = input("\nYou: ")
            
            if user_input.lower() in ("exit", "quit"):
                print("Exiting debug mode...")
                break
                
            if not user_input.strip():
                continue

            print("Lyra is thinking...")
            
            # Gọi hàm chat của MiniAI giống cách web.py gọi
            result = ai.chat(user_input)
            
            print("\nLyra:", result.get("reply", "[No reply]"))
            print("-" * 40)
            print(f"Emotion: {result.get('emotion')}")
            print(f"Mood: {result.get('mood')} | Affection: {result.get('affection')}")
            print(f"Time Gap: {result.get('time_gap_hours')} | Period: {result.get('time_period')}")
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
