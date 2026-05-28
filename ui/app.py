import sys
import os
import requests
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gradio as gr
from src.utils.logger import get_logger

logger = get_logger(__name__)


def respond(message: str, history: list, session_id: str):
    logger.info(f"UI respond event triggered for session: {session_id}")
    if not message.strip():
        return history, "", session_id
    
    try:
        res = requests.post(
            "http://127.0.0.1:8000/ask-anything", 
            json={"prompt": message, "session_id": session_id}
        )
        res.raise_for_status()
        reply = res.json().get("answer", "Error: No answer in response")
    except Exception as e:
        logger.error(f"API request failed: {e}")
        reply = f"Sorry, there was an error reaching the server: {e}"

    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    return history, "", session_id


def new_conversation():
    new_session = str(uuid.uuid4())
    logger.info(f"Starting new conversation from UI button. Session: {new_session}")
    return [], new_session

with gr.Blocks(title="Solstice Pilates — AI Receptionist") as demo:
    gr.Markdown("## Solstice Pilates\n**AI Receptionist** — Phase 1: Text Chat")

    # Explicit Web Call Button that triggers Javascript
    call_btn = gr.Button("📞 Start Solstice Pilates Voice Assistant Call", variant="primary", size="lg")
    
    js_vapi_loader = """
    function() {
        if (!window.vapiSDK) {
            var g = document.createElement('script');
            g.src = "https://cdn.jsdelivr.net/gh/VapiAI/html-script-tag@latest/dist/assets/index.js";
            g.onload = function () {
                window.vapiSDK.run({
                    apiKey: "8ffa38c7-ccf6-4e09-b33d-e9da471a69ae", // Vapi Public Key (usually starts with pub_)
                    assistant: "d5e002ab-f0f1-4a93-90d3-d3338657bb48", // Assistant ID
                });
                alert("Voice widget injected! Check the bottom right corner of the page.\\n\\n(If you don't see it, your API Key or Assistant ID is invalid).");
            };
            g.onerror = function() {
                alert("Failed to load Vapi SDK.");
            }
            document.head.appendChild(g);
        } else {
            alert("Voice widget is already active in the bottom right corner!");
        }
    }
    """
    call_btn.click(fn=None, inputs=[], outputs=[], js=js_vapi_loader)

    session_state = gr.State("")
    chatbot = gr.Chatbot(height=520, label="")

    with gr.Row():
        msg = gr.Textbox(
            placeholder="Ask about classes, bookings, pricing...",
            scale=5,
            show_label=False,
            autofocus=True,
        )
        send_btn = gr.Button("Send", scale=1, variant="primary")

    reset_btn = gr.Button("New Conversation", size="sm")

    gr.Examples(
        examples=[
            "Is the 6pm Reformer class on Thursday open?",
            "What classes do you have this week?",
            "How much is a drop-in?",
            "I need to reschedule my class",
            "Can my friend drop in tomorrow?",
            "I'm going to be a few minutes late to my class",
        ],
        inputs=msg,
    )

    send_btn.click(respond, [msg, chatbot, session_state], [chatbot, msg, session_state])
    msg.submit(respond, [msg, chatbot, session_state], [chatbot, msg, session_state])
    reset_btn.click(new_conversation, [], [chatbot, session_state])
    demo.load(new_conversation, [], [chatbot, session_state])


if __name__ == "__main__":
    demo.launch(show_error=True, theme=gr.themes.Base())
