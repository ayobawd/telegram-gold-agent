from fastapi import FastAPI, Request
import os
import requests

app = FastAPI()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

@app.post("/")
async def handle(request: Request):
    # Extract the message text from the incoming JSON payload
    try:
        data = await request.json()
    except Exception:
        data = None
    message = None
    if isinstance(data, dict):
        # Try common keys
        message = data.get("text") or data.get("message")
        # If messages list is present, join into a single string
        if not message and isinstance(data.get("messages"), list):
            message = "\n".join([str(m) for m in data["messages"]])
    # Fallback to string representation
    if not message:
        message = str(data) if data is not None else "No message provided"

    # Construct Telegram API URL
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    try:
        resp = requests.post(telegram_url, data=payload, timeout=10)
        return {"status": "sent", "telegram_status": resp.status_code}
    except Exception as e:
        return {"status": "error", "error": str(e)}
