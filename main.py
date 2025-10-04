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

print(f"[boot] Python: {sys.version}", flush=True)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_CHAT_ID = (os.getenv("CHAT_ID") or "").strip()
DEFAULT_PARSE_MODE = (os.getenv("DEFAULT_PARSE_MODE") or "").strip()  # e.g., "HTML" or "MarkdownV2"
STRICT_CHAT_ID = os.getenv("STRICT_CHAT_ID", "0") in ("1", "true", "True")
TELEGRAM_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

print(f"[boot] BOT_TOKEN set: {bool(BOT_TOKEN)}", flush=True)
print(f"[boot] CHAT_ID present: {bool(DEFAULT_CHAT_ID)} value='{DEFAULT_CHAT_ID}'", flush=True)
print(f"[boot] DEFAULT_PARSE_MODE: '{DEFAULT_PARSE_MODE}'", flush=True)
print(f"[boot] STRICT_CHAT_ID: {STRICT_CHAT_ID}", flush=True)

app = FastAPI(title="telegram-gold-endpoint", version="1.2.0")

# ---------------- utils ----------------

def _now_ms() -> int:
    return int(time.time() * 1000)

def _short(s: str, n: int = 800) -> str:
    if s is None:
        return "None"
    return s if len(s) <= n else s[:n] + f"... [truncated {len(s)-n} chars]"

def _as_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return f"<unserializable {type(obj).__name__}>"

def _is_numeric_chat_id(x: str) -> bool:
    if not x:
        return False
    if x.startswith("-"):
        return x[1:].isdigit()
    return x.isdigit()

def _telegram_call(method: str, params: Dict[str, Any], rid: str) -> Dict[str, Any]:
    if not TELEGRAM_BASE:
        raise HTTPException(status_code=503, detail="Service not ready: BOT_TOKEN missing")
    url = f"{TELEGRAM_BASE}/{method}"
    print(f"[{rid}][tg] {method} -> {url} params={_short(_as_json(params))}", flush=True)
    try:
        r = requests.post(url, data=params, timeout=15)  # Bot API prefers form data
    except requests.RequestException as e:
        print(f"[{rid}][tg][network][ERROR] {e}", flush=True)
        raise HTTPException(status_code=502, detail=f"Telegram request error: {e}")
    print(f"[{rid}][tg] http={r.status_code}", flush=True)
    try:
        j = r.json()
    except Exception:
        print(f"[{rid}][tg][nonjson] {_short(r.text)}", flush=True)
        return {"ok": False, "http_status": r.status_code, "raw": r.text}
    print(f"[{rid}][tg][json] {_short(_as_json(j))}", flush=True)
    return j

def _telegram_get(method: str, params: Dict[str, Any], rid: str) -> Dict[str, Any]:
    if not TELEGRAM_BASE:
        raise HTTPException(status_code=503, detail="Service not ready: BOT_TOKEN missing")
    url = f"{TELEGRAM_BASE}/{method}"
    print(f"[{rid}][tg] {method} (GET) -> {url} params={_short(_as_json(params))}", flush=True)
    try:
        r = requests.get(url, params=params, timeout=15)
    except requests.RequestException as e:
        print(f"[{rid}][tg][network][ERROR] {e}", flush=True)
        raise HTTPException(status_code=502, detail=f"Telegram request error: {e}")
    print(f"[{rid}][tg] http={r.status_code}", flush=True)
    try:
        j = r.json()
    except Exception:
        print(f"[{rid}][tg][nonjson] {_short(r.text)}", flush=True)
        return {"ok": False, "http_status": r.status_code, "raw": r.text}
    print(f"[{rid}][tg][json] {_short(_as_json(j))}", flush=True)
    return j

def resolve_chat_id(chat_ref: Optional[str], rid: str) -> str:
    ref = (chat_ref or DEFAULT_CHAT_ID or "").strip()
    if not ref:
        msg = "Missing chat_id (no CHAT_ID env and no chat_id in payload)."
        print(f"[{rid}][resolve_chat_id][ERROR] {msg}", flush=True)
        raise HTTPException(status_code=400, detail=msg)

    if _is_numeric_chat_id(ref):
        print(f"[{rid}][resolve_chat_id] Using numeric chat_id='{ref}'", flush=True)
        return ref

    if STRICT_CHAT_ID:
        print(f"[{rid}][resolve_chat_id][STRICT] Non-numeric chat_id='{ref}' rejected", flush=True)
        raise HTTPException(status_code=400, detail="CHAT_ID must be numeric in STRICT mode")

    if ref.startswith("@"):
        j = _telegram_get("getChat", {"chat_id": ref}, rid)
        if j.get("ok") and isinstance(j.get("result"), dict):
            cid = j["result"].get("id")
            if cid is not None:
                print(f"[{rid}][resolve_chat_id] Resolved '{ref}' -> {cid}", flush=True)
                return str(cid)
        print(f"[{rid}][resolve_chat_id][ERROR] Could not resolve '{ref}' via getChat", flush=True)
        raise HTTPException(status_code=400, detail={"error": "cannot_resolve_chat", "ref": ref, "telegram": j})

    help_msg = (
        "CHAT_ID must be numeric (e.g., 123456789 or -100xxxxxxxxxxxx). "
        "For public channels/groups you may use '@name' and it will be resolved."
    )
    print(f"[{rid}][resolve_chat_id][ERROR] Non-numeric chat id '{ref}'. {help_msg}", flush=True)
    raise HTTPException(status_code=400, detail=help_msg)

# ---------------- text extraction (now with 'raw') ----------------

PREFERRED_TEXT_KEYS: List[str] = [
    # direct, styled or plain
    "text", "message", "content",
    # common “finals” people use
    "raw",          # <--- NEW: honor raw string directly
    "analysis",     # if your agent uses this
    "html",         # if you send already-styled HTML
    "markdown",     # if you send MD and rely on parse_mode
]

def extract_text(data: Dict[str, Any], rid: str) -> str:
    # 1) Single-string keys (including 'raw')
    for k in PREFERRED_TEXT_KEYS:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            print(f"[{rid}][extract_text] key='{k}'", flush=True)
            return v.strip()

    # 2) messages: ["a","b",...]
    msgs = data.get("messages")
    if isinstance(msgs, list) and msgs:
        parts = [str(m).strip() for m in msgs if str(m).strip()]
        if parts:
            print(f"[{rid}][extract_text] messages[{len(parts)}]", flush=True)
            return "\n".join(parts)

    # 3) outputs: [{type, value}, ...]
    outs = data.get("outputs")
    if isinstance(outs, list) and outs:
        parts: List[str] = []
        for o in outs:
            if isinstance(o, dict):
                v = o.get("value")
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
        if parts:
            print(f"[{rid}][extract_text] outputs[{len(parts)}]", flush=True)
            return "\n\n".join(parts)

    # 4) Fallback: pretty JSON so nothing is lost
    print(f"[{rid}][extract_text] fallback pretty JSON", flush=True)
    return _as_json(data)

# ---------------- sending ----------------

def send_telegram(text: str, chat_id_ref: Optional[str], rid: str, parse_mode: Optional[str] = None) -> None:
    chat_id = resolve_chat_id(chat_id_ref, rid)

    # If caller didn't specify parse_mode, use DEFAULT_PARSE_MODE if set
    effective_parse_mode = parse_mode or (DEFAULT_PARSE_MODE if DEFAULT_PARSE_MODE in ("HTML", "Markdown", "MarkdownV2") else None)
    if effective_parse_mode:
        print(f"[{rid}][send] parse_mode={effective_parse_mode}", flush=True)

    MAX = 4000
    safe_text = text or "(empty message)"

    for i in range(0, len(safe_text), MAX):
        chunk = safe_text[i:i + MAX]
        params = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if effective_parse_mode:
            params["parse_mode"] = effective_parse_mode

        j1 = _telegram_call("sendMessage", params, rid)
        if j1.get("ok"):
            continue

        # Fallback if formatting is the issue
        desc = str(j1.get("description", "")).lower()
        raw = str(j1.get("raw", "")).lower()
        if any(k in (desc + " " + raw) for k in ("parse", "entities", "markdown", "html")) and "parse_mode" in params:
            print(f"[{rid}][send] formatting error; retry without parse_mode", flush=True)
            params.pop("parse_mode", None)
            j2 = _telegram_call("sendMessage", params, rid)
            if j2.get("ok"):
                continue
            raise HTTPException(status_code=502, detail={"stage": "telegram_fallback_failed", "first": j1, "second": j2})

        raise HTTPException(status_code=502, detail={"stage": "telegram_error", "response": j1})

# ---------------- routes ----------------

@app.get("/health")
def health(probe: Optional[int] = 0):
    ready = bool(BOT_TOKEN and TELEGRAM_BASE)
    info: Dict[str, Any] = {
        "ok": ready,
        "python": sys.version,
        "has_default_chat": bool(DEFAULT_CHAT_ID),
        "default_chat_id": DEFAULT_CHAT_ID if DEFAULT_CHAT_ID else None,
        "strict_chat_id": STRICT_CHAT_ID,
        "default_parse_mode": DEFAULT_PARSE_MODE or None,
    }
    if not ready:
        return info

    rid = uuid.uuid4().hex[:8]
    if probe:
        try:
            me = _telegram_get("getMe", {}, rid)
            info["getMe_ok"] = bool(me.get("ok"))
            info["bot_username"] = (me.get("result") or {}).get("username")
        except HTTPException as e:
            info["getMe_error"] = e.detail
        if DEFAULT_CHAT_ID and not _is_numeric_chat_id(DEFAULT_CHAT_ID) and DEFAULT_CHAT_ID.startswith("@"):
            try:
                cid = resolve_chat_id(DEFAULT_CHAT_ID, rid)
                info["resolved_default_chat_id"] = cid
            except HTTPException as e:
                info["resolve_error"] = e.detail
    return info

@app.post("/")
async def handle(request: Request):
    rid = uuid.uuid4().hex[:8]
    t0 = _now_ms()
    print(f"[{rid}][handle] incoming", flush=True)

    try:
        try:
            data = await request.json()
            if not isinstance(data, dict):
                data = {"raw": data}
        except Exception:
            body = await request.body()
            data = {"raw": body.decode("utf-8", errors="replace")}
        print(f"[{rid}][payload] {_short(_as_json(data), 1200)}", flush=True)

        text = extract_text(data, rid)
        chat_id_ref = str(data.get("chat_id") or "").strip() or None
        parse_mode = data.get("parse_mode")  # may be None

        # If the payload provided 'html' field, prefer HTML parse mode automatically
        if not parse_mode and isinstance(data.get("html"), str) and data["html"].strip():
            parse_mode = "HTML"

        print(f"[{rid}][resolved] chat_ref={'<payload>' if chat_id_ref else '<env/resolve>'} "
              f"parse_mode={parse_mode!r} text_len={len(text or '')}", flush=True)

        send_telegram(text, chat_id_ref, rid, parse_mode=parse_mode)

        dt = _now_ms() - t0
        print(f"[{rid}][done] sent duration_ms={dt}", flush=True)
        return JSONResponse({"ok": True, "status": "sent", "length": len(text or ""), "rid": rid})

    except HTTPException as e:
        dt = _now_ms() - t0
        print(f"[{rid}][HTTPException] code={e.status_code} detail={_short(_as_json(e.detail), 1000)} duration_ms={dt}", flush=True)
        return JSONResponse({"ok": False, "error": "HTTPException", "status_code": e.status_code, "detail": e.detail, "rid": rid},
                            status_code=e.status_code)
    except Exception as e:
        dt = _now_ms() - t0
        tb = traceback.format_exc()
        print(f"[{rid}][EXCEPTION] {e}\n{tb}\n[duration_ms={dt}]", flush=True)
        return JSONResponse({"ok": False, "error": "Exception", "detail": str(e), "rid": rid, "trace": tb.splitlines()[-10:]},
                            status_code=500)
