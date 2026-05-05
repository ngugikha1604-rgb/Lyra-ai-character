import os
import sys

# Thêm thư mục hiện tại vào path để import được
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from core import MiniAI
    print("Importing MiniAI...")
    ai = MiniAI()
    print("MiniAI initialized successfully!")
    
    # Thử gọi một hàm đơn giản nếu có thể, hoặc chỉ kiểm tra xem brain đã load chưa
    if hasattr(ai, 'brain') and ai.brain:
        print("DSPy Brain is active.")
    else:
        print("DSPy Brain is NOT active (Check logs).")
        
except Exception as e:
    print(f"Error during initialization: {e}")
    import traceback
    traceback.print_exc()
