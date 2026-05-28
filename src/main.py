"""
Solstice Pilates — AI Receptionist
Convenience entry point: starts the FastAPI server directly.

Usage:
    python -m src.main                         # API server on :8000
    uvicorn src.app:app --reload --port 8000   # same, with hot-reload

The Gradio chat UI is a separate process:
    python ui/app.py
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "src.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_config=None,   # don't let uvicorn override our logging setup
    )
