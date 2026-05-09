"""
main.py — Entry point thay thế web.py.

Chạy:
    python main.py
    # hoặc
    flask --app main run --port 5000
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,   # Tắt debug mode khi dùng threading + daemon threads
        threaded=True,
    )
