from __future__ import annotations

import os
import sys
import json
import time
import uuid
import traceback
from typing import Optional, Any, Dict

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# ===============================
# Boot diagnostics
# ===============================
print(f"[boot] Python: {sys.version}", flush=True)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_CHAT_ID = (os.getenv("CHAT_ID") or "").strip()  # may be numeric or @username
STRICT_CHAT_ID = os.getenv("STRICT_CHAT_ID", "0") in ("1", "true", "True")
TELEGRAM_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

print(f"[boot] BOT_TOKEN set: {bool(BOT_TOKEN)}", flush=True)
print(f"[boot] CHAT_ID present: {bool(DEFAULT_CHAT_ID)} value='{DEFAULT_CHAT_ID}'", flush=True)
print(f"[boot] STRICT_CHAT_ID: {STRICT_CHAT_ID}", flush=True)

app = FastAPI(title="telegram-gold-endpoint", version="1.1.0")

# ===============================
# Utils
# ===============================

def _now_ms() -> int:
    return int(time.time() * 1000)

def _short(s: str, n: int = 800) -> str:
    if s is None:
        return "None"
    if len(s) <= n:
        return s
    return s[:n] + f"... [truncated {len(s)-n} chars]"

def _as_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return f"<unserializable {type(obj).__name__}>"

def _is_numeric_chat_id(x: str) -> bool:
    if not x:
        return False
    # numeric ids may be negative (groups/channels). e.g., -100xxxxxxxxxxxx
    if x.startswith("-"):
        return x[1:].isdigit()
    return x.isdigit()

def _telegram_call(method: str, params: Dict[str, Any], rid: str) -> Dict[str, Any]:
    if not TELEGRAM_BASE:
        raise HTTPException(status_code=503, detail="Service not ready: BOT_TOKEN missing")
    url = f"{TELEGRAM_BASE}/{method}"
    print(f"[{rid}][tg] {method} -> {url} params={_short(_as_json(params))}", flush=True)
    try:
        r = requests.post(url, data=params, timeout=15)  # form-encoded for Bot API
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
    """
    Returns a numeric chat_id usable with sendMessage.
    Accepts:
      - numeric string (e.g., "123456789", "-100123...")
      - @username / @channelusername  (resolved via getChat)
      - None/"" -> falls back to DEFAULT_CHAT_ID
    """
    ref = (chat_ref or DEFAULT_CHAT_ID or "").strip()
    if not ref:
        msg = "Missing chat_id (no CHAT_ID env and no chat_id in payload)."
        print(f"[{rid}][resolve_chat_id][ERROR] {msg}", flush=True)
        raise HTTPException(status_code=400, detail=msg)

    # already numeric?
    if _is_numeric_chat_id(ref):
        print(f"[{rid}][resolve_chat_id] Using numeric chat_id='{ref}'", flush=True)
        return ref

    # strict mode forbids resolving @usernames
    if STRICT_CHAT_ID:
        print(f"[{rid}][resolve_chat_id][STRICT] Non-numeric chat_id='{ref}' rejected", flush=True)
        raise HTTPException(status_code=400, detail="CHAT_ID must be numeric in STRICT mode")

    # if starts with @, try getChat (works for public groups/channels; not for private user DMs)
    if ref.startswith("@"):
        j = _telegram_get("getChat", {"chat_id": ref}, rid)
        if j.get("ok") and isinstance(j.get("result"), dict):
            cid = j["result"].get("id")
            if cid is not None:
                print(f"[{rid}][resolve_chat_id] Resolved '{ref}' -> {cid}", flush=True)
                return str(cid)
        # resolution failed
        detail = j if isinstance(j, dict) else {"raw": str(j)}
        print(f"[{rid}][resolve_chat_id][ERROR] Could not resolve '{ref}' via getChat", flush=True)
        raise HTTPException(status_code=400, detail={"error": "cannot_resolve_chat", "ref": ref, "telegram": detail})

    # any other string that isn't numeric -> reject with help
    help_msg = (
        "CHAT_ID must be a numeric ID (e.g., 123456789 for private, -100xxxxxxxxxxxx for channels). "
        "If you meant a public channel/group, prefix with '@' and I will resolve it via getChat."
    )
    print(f"[{rid}][resolve_chat_id][ERROR] Non-numeric chat id '{ref}'. {help_msg}", flush=True)
    raise HTTPException(status_code=400, detail=help_msg)

def extract_text(data: Dict[str, Any], rid: str) -> str:
    for k in ("text", "message", "content"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            print(f"[{rid}][extract_text] key='{k}'", flush=True)
            return v.strip()

    msgs = data.get("messages")
    if isinstance(msgs, list) and msgs:
        parts = [str(m).strip() for m in msgs if str(m).strip()]
        if parts:
            print(f"[{rid}][extract_text] messages[{len(parts)}]", flush=True)
            return "\n".join(parts)

    outs = data.get("outputs")
    if isinstance(outs, list) and outs:
        parts = []
        for o in outs:
            if isinstance(o, dict):
                v = o.get("value")
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
        if parts:
            print(f"[{rid}][extract_text] outputs[{len(parts)}]", flush=True)
            return "\n\n".join(parts)

    print(f"[{rid}][extract_text] fallback pretty JSON", flush=True)
    return _as_json(data)

def send_telegram(text: str, chat_id_ref: Optional[str], rid: str, parse_mode: Optional[str] = None) -> None:
    # Resolve chat id first (handles env/default + @username resolution)
    chat_id = resolve_chat_id(chat_id_ref, rid)

    MAX = 4000
    safe_text = text or "(empty message)"

    for i in range(0, len(safe_text), MAX):
        chunk = safe_text[i:i + MAX]

        # First attempt with parse_mode if provided
        params = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode in ("Markdown", "MarkdownV2", "HTML"):
            params["parse_mode"] = parse_mode

        j1 = _telegram_call("sendMessage", params, rid)
        if j1.get("ok"):
            continue  # next chunk

        # If formatting likely at fault, retry without parse_mode
        desc = str(j1.get("description", "")).lower()
        raw = str(j1.get("raw", "")).lower()
        if any(k in (desc + " " + raw) for k in ("parse", "entities", "markdown", "html")) and "parse_mode" in params:
            print(f"[{rid}][send] formatting error detected; retrying without parse_mode", flush=True)
            params.pop("parse_mode", None)
            j2 = _telegram_call("sendMessage", params, rid)
            if j2.get("ok"):
                continue
            raise HTTPException(status_code=502, detail={"stage": "telegram_fallback_failed", "first": j1, "second": j2})

        # Non-formatting failure
        raise HTTPException(status_code=502, detail={"stage": "telegram_error", "response": j1})

# ===============================
# Routes
# ===============================

@app.get("/health")
def health(probe: Optional[int] = 0):
    ready = bool(BOT_TOKEN and TELEGRAM_BASE)
    info: Dict[str, Any] = {
        "ok": ready,
        "python": sys.version,
        "has_default_chat": bool(DEFAULT_CHAT_ID),
        "default_chat_id": DEFAULT_CHAT_ID if DEFAULT_CHAT_ID else None,
        "strict_chat_id": STRICT_CHAT_ID,
    }
    if not ready:
        return info

    # Optional probe for deeper checks
    rid = uuid.uuid4().hex[:8]
    if probe:
        try:
            me = _telegram_get("getMe", {}, rid)
            info["getMe_ok"] = bool(me.get("ok"))
            info["bot_username"] = (me.get("result") or {}).get("username")
        except HTTPException as e:
            info["getMe_error"] = e.detail
        # If we have a default chat and it's not numeric, try resolve (doesn't send a message)
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
            data = {"raw_bytes": body.decode("utf-8", errors="replace")}
        print(f"[{rid}][payload] {_short(_as_json(data), 1200)}", flush=True)

        text = extract_text(data, rid)
        chat_id_ref = str(data.get("chat_id") or "").strip() or None
        parse_mode = data.get("parse_mode")

        print(f"[{rid}][resolved] chat_ref={'<payload>' if chat_id_ref else '<env/resolve>'} "
              f"parse_mode={parse_mode!r} text_len={len(text or '')}", flush=True)

        send_telegram(text, chat_id_ref, rid, parse_mode=parse_mode)

        dt = _now_ms() - t0
        print(f"[{rid}][done] sent duration_ms={dt}", flush=True)
        return JSONResponse({"ok": True, "status": "sent", "length": len(text or ""), "rid": rid})

    except HTTPException as e:
        dt = _now_ms() - t0
        print(f"[{rid}][HTTPException] code={e.status_code} detail={_short(_as_json(e.detail), 1000)} "
              f"duration_ms={dt}", flush=True)
        return JSONResponse({"ok": False, "error": "HTTPException", "status_code": e.status_code, "detail": e.detail, "rid": rid},
                            status_code=e.status_code)
    except Exception as e:
        dt = _now_ms() - t0
        tb = traceback.format_exc()
        print(f"[{rid}][EXCEPTION] {e}\n{tb}\n[duration_ms={dt}]", flush=True)
        return JSONResponse({"ok": False, "error": "Exception", "detail": str(e), "rid": rid, "trace": tb.splitlines()[-10:]},
                            status_code=500)
