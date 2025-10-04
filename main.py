import os, json, requests
from typing import Optional
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_CHAT_ID = os.getenv("CHAT_ID")  # optional default

if not BOT_TOKEN:
    # Fail fast so bad deployments are obvious in logs
    raise RuntimeError("BOT_TOKEN env var is required")

TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

def extract_text(data: dict) -> str:
    # Common keys first
    for k in ("text", "message", "content"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # messages: ["a","b",...]
    msgs = data.get("messages")
    if isinstance(msgs, list) and msgs:
        parts = [str(m).strip() for m in msgs if str(m).strip()]
        if parts:
            return "\n".join(parts)

    # OnDemand style: outputs: [{type, value}, ...]
    outs = data.get("outputs")
    if isinstance(outs, list) and outs:
        parts = []
        for o in outs:
            if isinstance(o, dict):
                v = o.get("value")
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
        if parts:
            return "\n\n".join(parts)

    # Fallback: pretty JSON
    return json.dumps(data, ensure_ascii=False, indent=2)

def send_telegram(text: str, chat_id: str, parse_mode: Optional[str] = None):
    if not chat_id:
        raise HTTPException(status_code=400, detail="Missing chat_id (no CHAT_ID env and no chat_id in payload).")

    # Telegram limit is ~4096 chars; send in safe chunks
    MAX = 4000
    safe_text = text or "(empty message)"
    for i in range(0, len(safe_text), MAX):
        payload = {
            "chat_id": chat_id,
            "text": safe_text[i:i+MAX],
            "disable_web_page_preview": True,
        }
        if parse_mode in ("Markdown", "MarkdownV2", "HTML"):
            payload["parse_mode"] = parse_mode

        try:
            r = requests.post(TELEGRAM_URL, json=payload, timeout=15)
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"Telegram request error: {e}")

        if not r.ok:
            raise HTTPException(status_code=502, detail=f"Telegram HTTP error: {r.status_code} {r.text}")
        try:
            j = r.json()
        except Exception:
            raise HTTPException(status_code=502, detail=f"Telegram non-JSON response: {r.text}")
        if not j.get("ok", False):
            raise HTTPException(status_code=502, detail=f"Telegram API error: {j}")

@app.get("/health")
def health():
    return {"ok": True, "has_default_chat": bool(DEFAULT_CHAT_ID)}

@app.post("/")
async def handle(request: Request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            data = {"raw": data}
    except Exception:
        data = {}

    text = extract_text(data)
    # Allow overrides from payload; fall back to env
    chat_id = str(data.get("chat_id") or DEFAULT_CHAT_ID or "").strip()
    parse_mode = data.get("parse_mode")  # optional: "MarkdownV2", "HTML", etc.

    send_telegram(text, chat_id, parse_mode=parse_mode)
    return {"status": "sent", "to": chat_id or "(none)", "length": len(text or "")}
