from __future__ import annotations

import os
import sys
import json
import time
import uuid
import traceback
from typing import Optional, Any, Dict, List

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# -----------------------
# Boot diagnostics
# -----------------------
print(f"[boot] Python: {sys.version}", flush=True)
print(f"[boot] BOT_TOKEN set: {bool(os.getenv('BOT_TOKEN'))}", flush=True)
print(f"[boot] CHAT_ID set: {bool(os.getenv('CHAT_ID'))}", flush=True)
print(f"[boot] ENV PORT: {os.getenv('PORT')}", flush=True)

app = FastAPI(title="telegram-gold-endpoint", version="1.0.0")

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_CHAT_ID = os.getenv("CHAT_ID")  # optional default
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage" if BOT_TOKEN else None

if not BOT_TOKEN:
    # Fail fast so bad deployments are obvious in logs
    # (FastAPI will still start; but health will say not ready)
    print("[boot][ERROR] BOT_TOKEN env var is required", flush=True)

# -----------------------
# Helpers
# -----------------------

def _now_ms() -> int:
    return int(time.time() * 1000)

def _shorten(s: str, n: int = 600) -> str:
    if s is None:
        return "None"
    if len(s) <= n:
        return s
    return s[:n] + f"... [truncated {len(s)-n} chars]"

def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return f"<unserializable: {type(obj).__name__}>"

def extract_text(data: Dict[str, Any], rid: str) -> str:
    # Common keys first
    for k in ("text", "message", "content"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            print(f"[{rid}][extract_text] Using key '{k}'", flush=True)
            return v.strip()

    # messages: ["a","b",...]
    msgs = data.get("messages")
    if isinstance(msgs, list) and msgs:
        parts = [str(m).strip() for m in msgs if str(m).strip()]
        if parts:
            print(f"[{rid}][extract_text] Using 'messages' list with {len(parts)} items", flush=True)
            return "\n".join(parts)

    # OnDemand style: outputs: [{type, value}, ...]
    outs = data.get("outputs")
    if isinstance(outs, list) and outs:
        parts: List[str] = []
        for o in outs:
            if isinstance(o, dict):
                v = o.get("value")
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
        if parts:
            print(f"[{rid}][extract_text] Using 'outputs' list with {len(parts)} items", flush=True)
            return "\n\n".join(parts)

    # Fallback: pretty JSON
    print(f"[{rid}][extract_text] Falling back to pretty JSON of payload", flush=True)
    return _safe_json(data)

def _telegram_post(payload: Dict[str, Any], rid: str) -> Dict[str, Any]:
    """
    Single POST attempt to Telegram with rich diagnostics.
    Returns parsed JSON (or a dict with 'raw' on parse error).
    Raises HTTPException on network-level problems.
    """
    if not TELEGRAM_URL:
        raise HTTPException(status_code=503, detail="Service not ready: missing BOT_TOKEN")

    print(f"[{rid}][telegram][request] {TELEGRAM_URL} payload={_shorten(_safe_json(payload), 800)}", flush=True)

    try:
        r = requests.post(TELEGRAM_URL, json=payload, timeout=15)
    except requests.RequestException as e:
        print(f"[{rid}][telegram][network][ERROR] {e}", flush=True)
        raise HTTPException(status_code=502, detail=f"Telegram request error: {e}")

    print(f"[{rid}][telegram][response] http_status={r.status_code}", flush=True)

    try:
        j = r.json()
        print(f"[{rid}][telegram][response][json] {_shorten(_safe_json(j), 800)}", flush=True)
        return j
    except Exception:
        raw = r.text
        print(f"[{rid}][telegram][response][nonjson] {_shorten(raw, 800)}", flush=True)
        return {"ok": False, "raw": raw, "http_status": r.status_code}

def send_telegram(text: str, chat_id: str, rid: str, parse_mode: Optional[str] = None) -> None:
    if not chat_id:
        msg = "Missing chat_id (no CHAT_ID env and no chat_id in payload)."
        print(f"[{rid}][send_telegram][ERROR] {msg}", flush=True)
        raise HTTPException(status_code=400, detail=msg)

    safe_text = text or "(empty message)"
    MAX = 4000  # below Telegram hard 4096

    # Chunked send for long messages
    for i in range(0, len(safe_text), MAX):
        chunk = safe_text[i:i+MAX]
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode in ("Markdown", "MarkdownV2", "HTML"):
            payload["parse_mode"] = parse_mode

        # First attempt
        j1 = _telegram_post(payload, rid)

        # Successful JSON response path
        if isinstance(j1, dict) and j1.get("ok", False):
            continue  # next chunk

        # Decide if we should fallback (likely formatting issues)
        desc = ""
        if isinstance(j1, dict):
            desc = str(j1.get("description", "")).lower()

        http_status = j1.get("http_status") if isinstance(j1, dict) else None
        raw = j1.get("raw") if isinstance(j1, dict) else None
        text_to_scan = f"{desc} {raw}".lower()

        if ("parse" in text_to_scan) or ("entities" in text_to_scan) or ("markdown" in text_to_scan) or ("html" in text_to_scan):
            # Retry without parse_mode
            print(f"[{rid}][send_telegram] Detected formatting error; retrying without parse_mode", flush=True)
            payload.pop("parse_mode", None)
            j2 = _telegram_post(payload, rid)
            if isinstance(j2, dict) and j2.get("ok", False):
                continue

            # Still bad — raise with full diagnostics
            raise HTTPException(
                status_code=502,
                detail={
                    "stage": "telegram_fallback_failed",
                    "first_attempt": j1,
                    "second_attempt": j2,
                },
            )

        # Not a formatting error — bubble up original failure
        raise HTTPException(
            status_code=502,
            detail={"stage": "telegram_error", "response": j1},
        )

# -----------------------
# Routes
# -----------------------

@app.get("/health")
def health():
    ready = bool(BOT_TOKEN) and bool(TELEGRAM_URL)
    return {
        "ok": ready,
        "python": sys.version,
        "has_default_chat": bool(DEFAULT_CHAT_ID),
        "telegram_url_set": bool(TELEGRAM_URL),
    }

@app.post("/")
async def handle(request: Request):
    rid = uuid.uuid4().hex[:8]
    t0 = _now_ms()
    print(f"[{rid}][handle] Incoming request", flush=True)

    try:
        try:
            data = await request.json()
            # Some platforms send primitives/arrays; normalize
            if not isinstance(data, dict):
                data = {"raw": data}
        except Exception:
            # Body isn’t JSON — try as text
            body_text = await request.body()
            data = {"raw_bytes": body_text.decode("utf-8", errors="replace")}
        print(f"[{rid}][handle][payload] {_shorten(_safe_json(data), 1200)}", flush=True)

        text = extract_text(data, rid)
        chat_id = str(data.get("chat_id") or DEFAULT_CHAT_ID or "").strip()
        parse_mode = data.get("parse_mode")  # "MarkdownV2", "HTML", etc.

        print(f"[{rid}][handle][resolved] chat_id={'<set>' if chat_id else '<missing>'} "
              f"parse_mode={parse_mode!r} text_len={len(text or '')}", flush=True)

        send_telegram(text, chat_id, rid, parse_mode=parse_mode)

        dt = _now_ms() - t0
        print(f"[{rid}][handle][done] status=sent duration_ms={dt}", flush=True)
        return JSONResponse({"ok": True, "status": "sent", "to": chat_id or "(none)", "length": len(text or ""), "rid": rid})

    except HTTPException as e:
        dt = _now_ms() - t0
        print(f"[{rid}][handle][HTTPException] code={e.status_code} detail={_shorten(_safe_json(e.detail), 1000)} "
              f"duration_ms={dt}", flush=True)
        return JSONResponse(
            {"ok": False, "error": "HTTPException", "status_code": e.status_code, "detail": e.detail, "rid": rid},
            status_code=e.status_code,
        )
    except Exception as e:
        dt = _now_ms() - t0
        tb = traceback.format_exc()
        print(f"[{rid}][handle][EXCEPTION] {e}\n{tb}\n[duration_ms={dt}]", flush=True)
        return JSONResponse(
            {"ok": False, "error": "Exception", "detail": str(e), "rid": rid, "trace": tb.splitlines()[-10:]},
            status_code=500,
        )
