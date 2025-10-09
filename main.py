from __future__ import annotations

import os, sys, json, time, uuid, re, traceback, threading
from typing import Optional, Any, Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

print(f"[boot] Python: {sys.version}", flush=True)

# --- Config ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_CHAT_ID = (os.getenv("CHAT_ID") or "").strip()
SECOND_DEFAULT_CHAT_ID = (os.getenv("CHAT_IDD") or "").strip()  # NEW
DEFAULT_PARSE_MODE = (os.getenv("DEFAULT_PARSE_MODE") or "").strip()  # e.g., "HTML" or "MarkdownV2"
STRICT_CHAT_ID = os.getenv("STRICT_CHAT_ID", "0") in ("1", "true", "True")
AUTO_FORMAT_RAW = os.getenv("AUTO_FORMAT_RAW", "0") in ("1", "true", "True")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
CHAT_REGISTRY_FILE = os.getenv("CHAT_REGISTRY_FILE", "/tmp/chat_ids.json")
BROADCAST_DEFAULT = os.getenv("BROADCAST_DEFAULT", "0") in ("1", "true", "True")

TELEGRAM_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

print(f"[boot] BOT_TOKEN set: {bool(BOT_TOKEN)}", flush=True)
print(f"[boot] CHAT_ID present: {bool(DEFAULT_CHAT_ID)} value='{DEFAULT_CHAT_ID}'", flush=True)
print(f"[boot] CHAT_IDD present: {bool(SECOND_DEFAULT_CHAT_ID)} value='{SECOND_DEFAULT_CHAT_ID}'", flush=True)  # NEW
print(f"[boot] DEFAULT_PARSE_MODE: '{DEFAULT_PARSE_MODE}'", flush=True)
print(f"[boot] STRICT_CHAT_ID: {STRICT_CHAT_ID}", flush=True)
print(f"[boot] AUTO_FORMAT_RAW: {AUTO_FORMAT_RAW}", flush=True)
print(f"[boot] WEBHOOK_SECRET set: {bool(WEBHOOK_SECRET)}", flush=True)
print(f"[boot] CHAT_REGISTRY_FILE: {CHAT_REGISTRY_FILE}", flush=True)
print(f"[boot] BROADCAST_DEFAULT: {BROADCAST_DEFAULT}", flush=True)

app = FastAPI(title="telegram-gold-endpoint", version="2.0.0")

# ---------------- Registry (thread-safe) ----------------

class ChatRegistry:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data = {"chats": {}}  # {chat_id: {type, title/username, first_seen, last_seen}}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                    if "chats" not in self._data or not isinstance(self._data["chats"], dict):
                        self._data = {"chats": {}}
        except Exception as e:
            print(f"[registry][load][ERROR] {e}", flush=True)
            self._data = {"chats": {}}

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[registry][save][ERROR] {e}", flush=True)

    def list_ids(self) -> List[str]:
        with self._lock:
            return list(self._data["chats"].keys())

    def list_full(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def add(self, chat_id: int | str, meta: Dict[str, Any]):
        cid = str(chat_id)
        now = int(time.time())
        with self._lock:
            rec = self._data["chats"].get(cid) or {}
            rec.update({
                "type": meta.get("type"),
                "title": meta.get("title"),
                "username": meta.get("username"),
                "first_seen": rec.get("first_seen", now),
                "last_seen": now,
            })
            self._data["chats"][cid] = rec
            self._save()
        print(f"[registry] saved chat {cid} meta={rec}", flush=True)

    def remove(self, chat_id: str):
        with self._lock:
            if chat_id in self._data["chats"]:
                del self._data["chats"][chat_id]
                self._save()

registry = ChatRegistry(CHAT_REGISTRY_FILE)

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
        r = requests.post(url, data=params, timeout=15)  # form data
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
        msg = "Missing chat_id (no CHAT_ID env, no chat_id in payload, and no saved chats)."
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

# ---------------- text extraction & optional auto-format ----------------

PREFERRED_TEXT_KEYS: List[str] = [
    "text", "message", "content",
    "raw", "analysis", "html", "markdown",
]

def extract_text(data: Dict[str, Any], rid: str) -> str:
    for k in PREFERRED_TEXT_KEYS:
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
        parts: List[str] = []
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

_HTML_TAG_PATTERN = re.compile(r"</?\w+[^>]*>")
def is_html_like(s: str) -> bool:
    return bool(_HTML_TAG_PATTERN.search(s or ""))

MONEY = r"\$?\s?([0-9]{1,3}(?:[, ]?[0-9]{3})*(?:\.[0-9]+)?)"
PCT = r"([+-]?\d+(?:\.\d+)?)\s*%"

def _to_float_str(val: Optional[str]) -> Optional[str]:
    if not val: return None
    try:
        x = float(val.replace(",", "").replace(" ", ""))
        if x >= 1000:
            return f"{x:,.0f}"
        return f"{x:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return val

def parse_raw_gold(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"spot": None, "d1": None, "low": None, "high": None, "trend": None, "drivers": [], "watch": []}
    m = re.search(rf"\${MONEY}.*?(?:per\s+ounce|/oz)", text, re.IGNORECASE)
    if m: out["spot"] = _to_float_str(m.group(1))
    m = re.search(rf"(up|down)\s+(?:\$\s?\d+(?:\.\d+)?|{PCT})", text, re.IGNORECASE)
    if m:
        dir_word, pct = m.group(1).lower(), m.group(2)
        if pct:
            out["d1"] = f"+{pct}%" if dir_word == "up" and not pct.startswith("+") else f"-{pct}%" if dir_word == "down" and not pct.startswith("-") else f"{pct}%"
        else:
            out["d1"] = "higher d/d" if dir_word == "up" else "lower d/d"
    else:
        if re.search(r"\bsteady|flat|minimal\b", text, re.IGNORECASE):
            out["d1"] = "steady d/d"
    m = re.search(rf"between\s+\${MONEY}\s+(?:and|-|to)\s+\${MONEY}", text, re.IGNORECASE) or \
        re.search(rf"range[d]?\s+(?:from\s+)?\${MONEY}\s+(?:to|-)\s+\${MONEY}", text, re.IGNORECASE)
    if m:
        out["low"] = _to_float_str(m.group(1)); out["high"] = _to_float_str(m.group(2))
    if re.search(r"\b(upward|higher|gains?|rebound(ed)?|positive)\b", text, re.IGNORECASE): out["trend"] = "Higher"
    elif re.search(r"\b(downward|lower|declin(e|ed)|negative)\b", text, re.IGNORECASE): out["trend"] = "Lower"
    elif re.search(r"\b(steady|flat|unchanged|minimal)\b", text, re.IGNORECASE): out["trend"] = "Flat"
    dm = re.search(r"Key drivers include\s+(.+?)(?:\.\s|$)", text, re.IGNORECASE | re.DOTALL)
    if dm:
        items = re.split(r",|\band\b", dm.group(1))
        out["drivers"] = [it.strip(" .;:") for it in items if it.strip(" .;:")]
    if re.search(r"safe[- ]?haven", text, re.IGNORECASE):
        out["drivers"].append("Safe-haven demand")
    wm = re.search(r"(Watch|Investors are watching|Near-term|Outlook)[^:]*[: ]\s*(.+)", text, re.IGNORECASE)
    if wm:
        items = re.split(r",|\band\b|;|\.\s", wm.group(2))
        out["watch"] = [it.strip(" .;:") for it in items if it.strip(" .;:")]
    out["drivers"] = list(dict.fromkeys(out["drivers"]))[:3]
    out["watch"] = list(dict.fromkeys(out["watch"]))[:2]
    return out

def format_gold_html(parsed: Dict[str, Any]) -> str:
    now = datetime.now(ZoneInfo("Asia/Dubai"))
    ts = now.strftime("%a, %H:%M GST")
    spot, d1, low, high, trend = parsed.get("spot"), parsed.get("d1"), parsed.get("low"), parsed.get("high"), parsed.get("trend")
    lines: List[str] = []
    lines.append("<b>Gold Market Update</b>")
    lines.append(f"<small>As of {ts}</small>")
    lines.append("")
    if spot or d1:
        lines.append(f"<b>Now:</b> ${spot} / oz ({d1})" if spot and d1 else f"<b>Now:</b> ${spot} / oz" if spot else f"<b>Now:</b> {d1}")
    if (low and high) or trend:
        parts = []
        if trend: parts.append(trend)
        if low and high: parts.append(f"Range {low} – {high}")
        if parts: lines.append(f"<b>7-day:</b> " + " | ".join(parts))
    if parsed.get("drivers"):
        lines.append(""); lines.append("<b>Drivers:</b>")
        for d in parsed["drivers"]: lines.append(f"– {d}")
    if parsed.get("watch"):
        lines.append(""); lines.append("<b>Outlook:</b>")
        for w in parsed["watch"]: lines.append(f"– {w}")
    msg = "\n".join(lines).strip()
    return msg if len(msg) <= 900 else (msg[:880] + "…")

def auto_format_if_plain(text: str, rid: str) -> (str, Optional[str]):
    if not AUTO_FORMAT_RAW: return text, None
    if not text or is_html_like(text): return text, None
    if "per ounce" in text.lower() or "/oz" in text.lower() or "gold" in text.lower():
        parsed = parse_raw_gold(text); html = format_gold_html(parsed)
        print(f"[{rid}][auto_format] applied -> HTML len={len(html)}", flush=True)
        return html, "HTML"
    return text, None

# ---------------- news time sanitizer ----------------
AR_DIGIT = r"[\u0660-\u0669]"
TZ_OPT = r"(?:\s?(?:GST|UTC|GMT|[A-Z]{2,4}))?"
EN_TIME = r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?"
AR_TIME = rf"(?:{AR_DIGIT}{{1,2}}:{AR_DIGIT}{{2}}(?::{AR_DIGIT}{{2}})?)"

_BRACKET_EN = re.compile(rf"\[(اليوم|آخر\s*7\s*أيام|مقرر)[^\]]*?{EN_TIME}{TZ_OPT}\]", re.IGNORECASE)
_BRACKET_AR = re.compile(rf"\[(اليوم|آخر\s*7\s*أيام|مقرر)[^\]]*?{AR_TIME}{TZ_OPT}\]", re.IGNORECASE)
_FREE_TIME = re.compile(rf"(?:\s*[؛,،\-—:]\s*)?(?:{EN_TIME}|{AR_TIME}){TZ_OPT}", re.IGNORECASE)

def _normalize_tags(s: str) -> str:
    s = re.sub(r"\s*\[\s*اليوم\s*؛?\s*\]", " [اليوم]", s)
    s = re.sub(r"\s*\[\s*آخر\s*7\s*أيام\s*؛?\s*\]", " [آخر 7 أيام]", s)
    s = re.sub(r"\s*\[\s*مقرر\s*؛?\s*\]", " [مقرر]", s)
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s*—\s*\]", "]", s)
    return s.rstrip()

def _is_news_line(line: str) -> bool:
    if line.startswith("📊"):
        return True
    if line.startswith("• ") and not line.startswith("• السعر الآن"):
        if line.strip().startswith("المصدر السعري:"):
            return False
        return True
    return False

def strip_time_from_news(text: str) -> str:
    lines = text.splitlines()
    out = []
    for ln in lines:
        if _is_news_line(ln):
            s = _BRACKET_EN.sub(r"[\1]", ln)
            s = _BRACKET_AR.sub(r"[\1]", s)
            s = _FREE_TIME.sub("", s)
            s = _normalize_tags(s)
            out.append(s)
        else:
            out.append(ln)
    return "\n".join(out)

# ---------------- sending ----------------

def send_telegram(text: str, chat_id_ref: Optional[str], rid: str, parse_mode: Optional[str] = None) -> None:
    chat_id = resolve_chat_id(chat_id_ref, rid)
    effective_parse_mode = parse_mode or (DEFAULT_PARSE_MODE if DEFAULT_PARSE_MODE in ("HTML", "Markdown", "MarkdownV2") else None)
    if effective_parse_mode: print(f"[{rid}][send] parse_mode={effective_parse_mode}", flush=True)
    MAX = 4000
    safe_text = text or "(empty message)"
    for i in range(0, len(safe_text), MAX):
        chunk = safe_text[i:i + MAX]
        params = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        if effective_parse_mode: params["parse_mode"] = effective_parse_mode
        j1 = _telegram_call("sendMessage", params, rid)
        if j1.get("ok"): continue
        desc = str(j1.get("description", "")).lower(); raw = str(j1.get("raw", "")).lower()
        if any(k in (desc + " " + raw) for k in ("parse", "entities", "markdown", "html")) and "parse_mode" in params:
            print(f"[{rid}][send] formatting error; retry without parse_mode", flush=True)
            params.pop("parse_mode", None)
            j2 = _telegram_call("sendMessage", params, rid)
            if j2.get("ok"): continue
            raise HTTPException(status_code=502, detail={"stage": "telegram_fallback_failed", "first": j1, "second": j2})
        raise HTTPException(status_code=502, detail={"stage": "telegram_error", "response": j1})

def send_to_many(text: str, chat_ids: List[str], rid: str, parse_mode: Optional[str] = None) -> Dict[str, Any]:
    results = {}
    for cid in chat_ids:
        try:
            send_telegram(text, cid, rid, parse_mode=parse_mode)
            results[cid] = "ok"
        except HTTPException as e:
            results[cid] = {"error": e.detail}
        except Exception as e:
            results[cid] = {"error": str(e)}
    return results

# ---------------- routes ----------------

@app.get("/health")
def health(probe: Optional[int] = 0):
    ready = bool(BOT_TOKEN and TELEGRAM_BASE)
    info: Dict[str, Any] = {
        "ok": ready,
        "python": sys.version,
        "has_default_chat": bool(DEFAULT_CHAT_ID),
        "default_chat_id": DEFAULT_CHAT_ID if DEFAULT_CHAT_ID else None,
        "has_second_default_chat": bool(SECOND_DEFAULT_CHAT_ID),     # NEW
        "second_default_chat_id": SECOND_DEFAULT_CHAT_ID or None,    # NEW
        "strict_chat_id": STRICT_CHAT_ID,
        "default_parse_mode": DEFAULT_PARSE_MODE or None,
        "auto_format_raw": AUTO_FORMAT_RAW,
        "saved_chats": registry.list_ids(),
    }
    if not ready: return info
    rid = uuid.uuid4().hex[:8]
    if probe:
        try:
            me = _telegram_get("getMe", {}, rid)
            info["getMe_ok"] = bool(me.get("ok"))
            info["bot_username"] = (me.get("result") or {}).get("username")
        except HTTPException as e:
            info["getMe_error"] = e.detail
    return info

@app.post("/webhook")
async def telegram_webhook(request: Request, secret: Optional[str] = None):
    """ Telegram will POST updates here. Use setWebhook with ?secret=... """
    rid = uuid.uuid4().hex[:8]
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        print(f"[{rid}][webhook][DENY] bad secret", flush=True)
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        update = await request.json()
    except Exception:
        update = {}
    print(f"[{rid}][webhook] {_short(_as_json(update), 1200)}", flush=True)

    # capture chat id from message or channel_post
    msg = update.get("message") or update.get("channel_post") or {}
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    ctype = chat.get("type")
    title = chat.get("title")
    username = chat.get("username")
    if cid:
        registry.add(cid, {"type": ctype, "title": title, "username": username})
    return {"ok": True}

@app.get("/chats")
def list_chats():
    return registry.list_full()

@app.post("/chats")
async def add_chat(payload: Dict[str, Any]):
    cid = str(payload.get("chat_id") or "").strip()
    if not _is_numeric_chat_id(cid):
        raise HTTPException(status_code=400, detail="chat_id must be numeric")
    meta = {
        "type": payload.get("type"),
        "title": payload.get("title"),
        "username": payload.get("username")
    }
    registry.add(cid, meta)
    return {"ok": True, "saved": cid}

@app.delete("/chats/{chat_id}")
def remove_chat(chat_id: str):
    registry.remove(chat_id)
    return {"ok": True, "removed": chat_id}

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
        parse_mode = data.get("parse_mode")

        # auto choose HTML if 'html' field exists
        if not parse_mode and isinstance(data.get("html"), str) and data["html"].strip():
            parse_mode = "HTML"

        # Auto-format plain to HTML if enabled
        formatted_text, auto_pm = auto_format_if_plain(text, rid)
        if auto_pm and not parse_mode:
            parse_mode = auto_pm
        text = formatted_text

        # 🔽 sanitize news lines to remove any times
        text = strip_time_from_news(text)

        # If no chat_id provided ➜ send to both defaults if available
        if not chat_id_ref:
            if BROADCAST_DEFAULT and registry.list_ids():
                targets = registry.list_ids()
                print(f"[{rid}][broadcast] -> {targets}", flush=True)
                results = send_to_many(text, targets, rid, parse_mode=parse_mode)
                dt = _now_ms() - t0
                return JSONResponse({"ok": True, "status": "broadcast", "results": results, "rid": rid, "duration_ms": dt})

            # Always attempt to send to both defaults if present
            targets: List[str] = []
            if DEFAULT_CHAT_ID:
                targets.append(DEFAULT_CHAT_ID)
            if SECOND_DEFAULT_CHAT_ID:
                targets.append(SECOND_DEFAULT_CHAT_ID)

            if targets:
                print(f"[{rid}][dual-default] -> {targets}", flush=True)
                results = send_to_many(text, targets, rid, parse_mode=parse_mode)
                dt = _now_ms() - t0
                return JSONResponse({"ok": True, "status": "sent_to_defaults", "results": results, "rid": rid, "duration_ms": dt})

            # No defaults and no registry
            raise HTTPException(status_code=400, detail="No chat_id provided, no default CHAT_ID/CHAT_IDD, and no saved chats to broadcast.")

        # If chat_id was provided in payload ➜ send only there (explicit override)
        print(f"[{rid}][resolved] chat_ref={'<payload>' if data.get('chat_id') else '<default>'} parse_mode={parse_mode!r} text_len={len(text or '')}", flush=True)
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
