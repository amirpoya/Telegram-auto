#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Global Poster Bot — COPY/FORWARD + Inline Buttons + Channel Edit (/attach) (Premium Emoji-safe)

✅ What this file does
- Works on python-telegram-bot v21+
- Render-ready health server (binds $PORT)
- Async handlers everywhere
- Stores settings in global_settings.json
- Supports premium emoji via MessageEntity(custom_emoji)
- Modes:
  • COPY   → copyMessage (clean, no "Forwarded from", supports buttons attached)
  • FORWARD→ forwardMessage (shows "Forwarded from", buttons are sent as a 2nd invisible-text message just under it)
- /import    → Reply to a message to capture it as the template (chat_id, message_id) + text/entities/photo
- /preview   → Preview current template or (text/entities) fallback in your DM
- /forward   → Reply to a message and forward it to all groups; then post buttons as a reply under it (hidden quote)
- /attach    → Edit the ORIGINAL channel post (template) and attach inline buttons to it (bot must be channel admin)
- /detach    → Remove inline buttons from the ORIGINAL channel post
- Flexible Buttons Input in menu (no JSON needed):
     Open - https://a.com
     Contact - @YourUser
     Open - https://a.com | Docs - https://b.com
  (JSON [["Open","https://a.com"], ...] still works too)

ENV REQUIRED:
  BOT_TOKEN   = ...
  OWNER_IDS   = "123,456"   (comma separated user ids)
OPTIONAL:
  PUBLIC_URL  = https://telegram-auto.onrender.com   (for keepalive pings)
"""

from __future__ import annotations
import os, re, json, asyncio, threading, http.server, socketserver
from typing import List, Dict, Any, Union
from urllib.parse import urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, ReplyParameters, InlineQueryResultArticle, InputTextMessageContent
from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    CallbackQueryHandler, InlineQueryHandler, filters
)
import aiohttp
import uuid

# ---------------- Health server (Render) ----------------

def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    print("Health server binding on PORT =", port, flush=True)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *a, **k):
            return

    def serve():
        with socketserver.TCPServer(("", port), Handler) as httpd:
            httpd.allow_reuse_address = True
            httpd.serve_forever()
    threading.Thread(target=serve, daemon=True).start()

# ---------------- Keepalive ----------------

async def _keepalive(context: ContextTypes.DEFAULT_TYPE):
    url = os.getenv("PUBLIC_URL", "").strip()
    if not url:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                await r.read()
    except Exception:
        pass

# ---------------- Credentials ----------------

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").strip().split(",") if x.strip().isdigit()}
if not TOKEN or not OWNER_IDS:
    raise SystemExit("BOT_TOKEN and OWNER_IDS env vars are required. Example OWNER_IDS='123,456'")

# ---------------- Storage ----------------

DATA_FILE = "global_settings.json"
DEFAULTS: Dict[str, Any] = {
    "message": "Hello! Scheduled message 🌟",
    "seconds": 15 * 60,
    "enabled": False,
    "groups": [],            # list[int]
    "buttons": [],           # list[[label, url]] or list[[[label,url],[label,url]], ...] rows
    "photo": None,           # file_id or url
    "entities": [],          # list[dict]
    "template": None,        # {"chat_id": int, "message_id": int}
    "template_has_keyboard": False,  # true if original message already includes inline keyboard
    "use_forward": False     # False=COPY, True=FORWARD
}

def load_store() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    else:
        data = {}
    for k, v in DEFAULTS.items():
        data.setdefault(k, v)
    if not isinstance(data.get("groups"), list):
        data["groups"] = []
    if not isinstance(data.get("buttons"), list):
        data["buttons"] = []
    if not isinstance(data.get("entities"), list):
        data["entities"] = []
    if data.get("template") is not None and not isinstance(data.get("template"), dict):
        data["template"] = None
    if not isinstance(data.get("use_forward"), bool):
        data["use_forward"] = False
    if not isinstance(data.get("template_has_keyboard"), bool):
        data["template_has_keyboard"] = False
    return data

store: Dict[str, Any] = load_store()

def save_store() -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)

# ---------------- Helpers ----------------

def is_owner(update: Update) -> bool:
    return (
        update.effective_chat
        and update.effective_chat.type == "private"
        and update.effective_user
        and update.effective_user.id in OWNER_IDS
    )

def parse_interval(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("m"): return int(float(s[:-1]) * 60)
    if s.endswith("h"): return int(float(s[:-1]) * 3600)
    if s.endswith("d"): return int(float(s[:-1]) * 86400)
    if re.fullmatch(r"\d+", s): return int(s)
    raise ValueError("Invalid interval. Example: 900 | 15m | 2h | 1d")

def _normalize_chat_ref(ref: str) -> Union[int, str]:
    ref = ref.strip()
    if not ref: raise ValueError("Empty reference.")
    if re.fullmatch(r"-?\d{6,}", ref): return int(ref)
    if ref.startswith("@"): return ref
    if ref.startswith("http://") or ref.startswith("https://"):
        u = urlparse(ref)
        if u.netloc.lower() != "t.me":
            raise ValueError("Only t.me links are supported.")
        parts = [p for p in u.path.split("/") if p]
        if not parts: raise ValueError("Bad t.me link.")
        if parts[0] == "c":
            if len(parts) < 2 or not parts[1].isdigit():
                raise ValueError("Bad t.me/c link.")
            internal = int(parts[1])
            return int(f"-100{internal}")
        if parts[0].startswith("+"):
            raise ValueError("Private invite links (+) can't be resolved by bot.")
        return "@" + parts[0]
    return "@" + ref

async def _resolve_chat_id(context: ContextTypes.DEFAULT_TYPE, ref: Union[int, str]) -> int:
    if isinstance(ref, int): return ref
    chat = await context.bot.get_chat(ref)
    return chat.id

# ---------- Flexible Buttons Parser ----------
BTN_SPLIT = re.compile(r"\s*(?:\||->|—>|—|-|→|:)\s+")
URL_RE = re.compile(r"^(?:https?://|tg://|mailto:|ftp://|\w+://)", re.I)

def _normalize_url(u: str) -> str:
    u = (u or '').strip()
    if not u: return u
    if u.startswith('@'): return f"https://t.me/{u[1:]}"
    if u.startswith('t.me/'): return f"https://{u}"
    return u if URL_RE.match(u) else f"https://{u}"

def parse_buttons_flexible(raw: str):
    raw = (raw or '').strip()
    if not raw: return []
    # Try JSON first
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    # Human-friendly lines
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('#'):
            continue
        parts = [p.strip() for p in line.split('|')] if '|' in line else [line]
        row = []
        for part in parts:
            toks = BTN_SPLIT.split(part, maxsplit=1)
            if len(toks) == 1:
                url = _normalize_url(toks[0])
                title = url.replace('https://','').replace('http://','')
            else:
                title, url = toks[0], _normalize_url(toks[1])
            if title and url:
                row.append([title, url])
        if row:
            rows.append(row if len(row) > 1 else row[0])
    return rows

# ---------- Entities helpers ----------

def _ent_to_dict(e: MessageEntity) -> dict:
    return {
        "type": e.type,
        "offset": e.offset,
        "length": e.length,
        "url": getattr(e, "url", None),
        "language": getattr(e, "language", None),
        "custom_emoji_id": getattr(e, "custom_emoji_id", None),
    }

async def _build_entities_from_store() -> List[MessageEntity]:
    ent_objs: List[MessageEntity] = []
    for d in store.get("entities", []):
        ent = MessageEntity(
            type=d.get("type"),
            offset=d.get("offset", 0),
            length=d.get("length", 0),
            url=d.get("url"),
            language=d.get("language"),
            custom_emoji_id=d.get("custom_emoji_id"),
            user=None,
        )
        ent_objs.append(ent)
    return ent_objs

# ---------- Keyboard builder (supports multi-column rows) ----------

def build_keyboard() -> InlineKeyboardMarkup | None:
    btns = store.get("buttons", [])
    if not btns:
        return None
    rows: List[List[InlineKeyboardButton]] = []
    for item in btns:
        if item and isinstance(item, list) and item and isinstance(item[0], list):
            rows.append([InlineKeyboardButton(text=l, url=u) for (l, u) in item])
        else:
            try:
                l, u = item
                rows.append([InlineKeyboardButton(text=l, url=u)])
            except Exception:
                continue
    return InlineKeyboardMarkup(rows) if rows else None

# ---------------- Broadcaster ----------------

INVISIBLE = "\u2060"  # zero-width no-break space (sticks tight, no preview)

async def send_to_all_groups(context: ContextTypes.DEFAULT_TYPE):
    if not store.get("enabled"):
        return

    kb = build_keyboard()
    tpl = store.get("template") if isinstance(store.get("template"), dict) else None

    # Preferred: use saved template
    if tpl and tpl.get("chat_id") and tpl.get("message_id"):
        for gid in list(dict.fromkeys(store.get("groups", []))):
            try:
                if store.get("use_forward"):
                    fwd = await context.bot.forward_message(
                        chat_id=gid, from_chat_id=tpl["chat_id"], message_id=tpl["message_id"]
                    )
                    # If original template already had its own keyboard, don't add ours
                    if kb:
                        try:
                            await context.bot.send_message(
                                chat_id=gid,
                                text=INVISIBLE,
                                reply_markup=kb,
                                reply_parameters=ReplyParameters(
                                    message_id=fwd.message_id,
                                    allow_sending_without_reply=True,
                                    quote=False,
                                ),
                            )
                        except Exception as e2:
                            print(f"[WARN] buttons failed for {gid}: {e2}")
                else:
                    await context.bot.copy_message(
                        chat_id=gid,
                        from_chat_id=tpl["chat_id"],
                        message_id=tpl["message_id"],
                        reply_markup=kb,
                    )
            except RetryAfter as e:
                await asyncio.sleep(int(e.retry_after) + 1)
                try:
                    if store.get("use_forward"):
                        fwd = await context.bot.forward_message(
                            chat_id=gid, from_chat_id=tpl["chat_id"], message_id=tpl["message_id"]
                        )
                        if kb:
                            try:
                                await context.bot.send_message(
                                    chat_id=gid,
                                    text=INVISIBLE,
                                    reply_markup=kb,
                                    reply_parameters=ReplyParameters(
                                        message_id=fwd.message_id,
                                        allow_sending_without_reply=True,
                                        quote=False,
                                    ),
                                )
                            except Exception as e2:
                                print(f"[WARN] buttons failed (retry) for {gid}: {e2}")
                    else:
                        await context.bot.copy_message(
                            chat_id=gid, from_chat_id=tpl["chat_id"], message_id=tpl["message_id"], reply_markup=kb
                        )
                except Exception as e2:
                    print(f"[WARN] retry failed for {gid}: {e2}")
            except (TimedOut, NetworkError) as e:
                print(f"[WARN] Network error for group {gid}: {e}")
                await asyncio.sleep(1)
            except Exception as e:
                print(f"[WARN] send failed for {gid}: {e}")
            await asyncio.sleep(0.5)
        return

    # Fallback: send from stored text/entities/photo
    msg_text: str = store.get("message", "")
    photo = store.get("photo")
    ent_objs = await _build_entities_from_store()

    for gid in list(dict.fromkeys(store.get("groups", []))):
        try:
            if photo:
                await context.bot.send_photo(
                    chat_id=gid, photo=photo, caption=msg_text,
                    reply_markup=kb, caption_entities=ent_objs if ent_objs else None,
                )
            else:
                await context.bot.send_message(
                    chat_id=gid, text=msg_text,
                    reply_markup=kb, entities=ent_objs if ent_objs else None,
                )
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            try:
                if photo:
                    await context.bot.send_photo(
                        chat_id=gid, photo=photo, caption=msg_text,
                        reply_markup=kb, caption_entities=ent_objs if ent_objs else None,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=gid, text=msg_text,
                        reply_markup=kb, entities=ent_objs if ent_objs else None,
                    )
            except Exception as e2:
                print(f"[WARN] retry failed for {gid}: {e2}")
        except (TimedOut, NetworkError) as e:
            print(f"[WARN] Network error for group {gid}: {e}")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"[WARN] send failed for {gid}: {e}")
        await asyncio.sleep(0.5)

# ---------------- Job scheduling ----------------

def reschedule_job(app: Application):
    for j in app.job_queue.get_jobs_by_name("GLOBAL_POSTER"):
        j.schedule_removal()
    if store.get("enabled"):
        app.job_queue.run_repeating(
            send_to_all_groups,
            interval=store.get("seconds", 900),
            first=0,
            name="GLOBAL_POSTER",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60},
        )

# ---------------- Menu & UX ----------------

def mode_badge() -> str:
    return "Forward" if store.get("use_forward") else "Copy"

def pretty_buttons() -> str:
    b = store.get("buttons", [])
    if not b: return "-"
    out = []
    for item in b:
        if item and isinstance(item, list) and item and isinstance(item[0], list):
            out.append(" | ".join([f"{l} → {u}" for l, u in item]))
        else:
            try:
                l, u = item
                out.append(f"{l} → {u}")
            except Exception:
                continue
    return "\n".join(out)

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("⚡ Status", callback_data="m:status"),
     InlineKeyboardButton("✅ Enable", callback_data="m:enable"),
     InlineKeyboardButton("⏹️ Disable", callback_data="m:disable")],
    [InlineKeyboardButton("⏰ Interval", callback_data="m:interval"),
     InlineKeyboardButton("✍️ Message", callback_data="m:message")],
    [InlineKeyboardButton("🖼️ Photo", callback_data="m:photo"),
     InlineKeyboardButton("🔘 Buttons", callback_data="m:buttons")],
    [InlineKeyboardButton("👥 Groups", callback_data="m:groups"),
     InlineKeyboardButton("🔁 Mode: Copy/Forward", callback_data="m:mode")],
    [InlineKeyboardButton("❓ Help", callback_data="m:help")],
])

def back_menu_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="m:menu")]])

def status_text() -> str:
    mins = store.get("seconds", 0) // 60
    tpl = store.get("template")
    tpl_txt = f"{tpl.get('chat_id')}:{tpl.get('message_id')}" if isinstance(tpl, dict) else "None"
    kb_flag = "Yes" if store.get("template_has_keyboard") else "No"
    return (
        f"✨ <b>Status:</b> {'Enabled ✅' if store.get('enabled') else 'Disabled ⏹️'}\n"
        f"⏰ Interval: <code>{store.get('seconds')}</code> sec (~{mins} min)\n"
        f"🔁 Mode: <b>{mode_badge()}</b>\n"
        f"🧩 Template: <code>{tpl_txt}</code>\n"
        f"🧷 Template has its own buttons: <b>{kb_flag}</b>\n"
        f"🖼️ Photo: <code>{store.get('photo') or 'None'}</code>\n"
        f"✍️ Message:\n<code>{(store.get('message') or '').replace('<','&lt;').replace('>','&gt;')}</code>\n"
        f"\n🔘 Buttons:\n{pretty_buttons()}\n"
        f"\n👥 Groups count: <b>{len(store.get('groups', []))}</b>"
    )

# ---------------- Commands ----------------

async def cmd_attach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Attach inline buttons to the ORIGINAL template post (e.g., in a channel).
    Requirements: Bot must be admin of that channel with 'Edit messages' permission.
    """
    if not is_owner(update):
        return
    tpl = store.get("template")
    if not (isinstance(tpl, dict) and tpl.get("chat_id") and tpl.get("message_id")):
        await update.message.reply_text("No template set. Use /import by replying to the target post (or forward it here) first.")
        return
    kb = build_keyboard()
    if not kb:
        await update.message.reply_text("No buttons set. Go to 🔘 Buttons in menu and define them first.")
        return
    try:
        await context.bot.edit_message_reply_markup(chat_id=tpl["chat_id"], message_id=tpl["message_id"], reply_markup=kb)
        store["template_has_keyboard"] = True
        save_store()
        await update.message.reply_text("Attached inline buttons to the original post ✅")
    except BadRequest as e:
        await update.message.reply_text(
        f"Edit failed: {e}
Make sure the bot is an admin of that channel with 'Edit messages' permission, and the post is editable."
    )
    except Exception as e:
        await update.message.reply_text(f"Edit failed: {e}")

async def cmd_detach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove inline buttons from the ORIGINAL template post."""
    if not is_owner(update):
        return
    tpl = store.get("template")
    if not (isinstance(tpl, dict) and tpl.get("chat_id") and tpl.get("message_id")):
        await update.message.reply_text("No template set. Use /import first.")
        return
    try:
        await context.bot.edit_message_reply_markup(chat_id=tpl["chat_id"], message_id=tpl["message_id"], reply_markup=None)
        store["template_has_keyboard"] = False
        save_store()
        await update.message.reply_text("Removed inline buttons from the original post ✅")
    except BadRequest as e:
        await update.message.reply_text(f"Remove failed: {e}")
    except Exception as e:
        await update.message.reply_text(f"Remove failed: {e}")

# ---------------- Commands ----------------

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg in {"copy", "forward"}:
        store["use_forward"] = (arg == "forward")
        save_store()
        await update.message.reply_text(f"Mode set to: {'Forward' if store['use_forward'] else 'Copy'} ✅")
    else:
        await update.message.reply_text("Usage: /mode copy  or  /mode forward")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_owner(update):
        await update.message.reply_text("Hi! Only bot owners can change settings.")
        return
    await update.message.reply_text("🌟 Bot Management Menu:", reply_markup=MAIN_MENU, parse_mode="HTML")

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text("🌟 Bot Management Menu:", reply_markup=MAIN_MENU, parse_mode="HTML")

async def on_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (update.effective_user and update.effective_user.id in OWNER_IDS):
        return
    q = update.callback_query
    data = (q.data or "").strip()
    context.user_data.clear()
    try: await q.answer()
    except Exception: pass

    async def safe_edit(text: str, **kwargs):
        try: return await q.edit_message_text(text, **kwargs)
        except BadRequest as e:
            if "Message is not modified" in str(e): return
            raise

    if data == "m:status":
        await safe_edit(status_text(), reply_markup=MAIN_MENU, parse_mode="HTML"); return
    if data == "m:enable":
        store["enabled"] = True; save_store(); reschedule_job(context.application)
        await safe_edit("Auto-posting enabled ✅", reply_markup=MAIN_MENU); return
    if data == "m:disable":
        store["enabled"] = False; save_store(); reschedule_job(context.application)
        await safe_edit("Auto-posting disabled ⏹️", reply_markup=MAIN_MENU); return
    if data == "m:interval":
        context.user_data["mode"] = "set_interval"
        await safe_edit(
            "Send new interval. Examples: <code>900</code> (sec) / <code>15m</code> / <code>2h</code>",
            reply_markup=back_menu_kb(), parse_mode="HTML",
        ); return
    if data == "m:message":
        context.user_data["mode"] = "set_message"
        await safe_edit(
            "Send the new message text. Any formatting / premium emojis will be captured.",
            reply_markup=back_menu_kb(),
        ); return
    if data == "m:photo":
        context.user_data["mode"] = "set_photo"
        await safe_edit("Send a photo (as photo upload) to set. Send 'none' to clear.", reply_markup=back_menu_kb()); return
    if data == "m:buttons":
        context.user_data["mode"] = "set_buttons"
        await safe_edit(
            (
                "Send buttons in ANY of these formats:\n\n"
                "1) One per line:  <code>Open - https://example.com</code>\n"
                "2) Multiple per row (use | ):  <code>Open - https://a.com | Docs - https://b.com</code>\n"
                "3) Username:  <code>Contact - @YourUser</code>\n"
                "4) Or JSON (optional). Missing scheme → https://\n"
            ),
            reply_markup=back_menu_kb(), parse_mode="HTML",
        ); return
    if data == "m:groups":
        context.user_data["mode"] = "set_groups"
        await safe_edit(
            "Send chat refs per line: -100123..., @publicname, or t.me/c/<id> (prefix '-' to remove)",
            reply_markup=back_menu_kb(),
        ); return
    if data == "m:mode":
        store["use_forward"] = not store.get("use_forward"); save_store()
        await safe_edit(f"Mode switched to <b>{mode_badge()}</b>.", reply_markup=MAIN_MENU, parse_mode="HTML"); return
    if data == "m:help":
        await safe_edit(
            (
                "<b>Help & tips</b>

"
                "- /import: Reply to your template to capture it.
"
                "- /forward: Reply to a message → forward to all groups, then send buttons under it.
"
                "- Copy mode supports buttons attached; Forward mode uses a reply-with-buttons.
"
                "- Premium emojis are preserved (via copy/forward).
"
                "- /attach and /detach let you add/remove inline buttons directly on the original channel post.
"
                "- Tip: use /mode copy or /mode forward to switch quickly.
"
            ),
            reply_markup=MAIN_MENU,
            parse_mode="HTML",
        ); return 
    if data == "m:menu":
        await safe_edit("🌟 Bot Management Menu:", reply_markup=MAIN_MENU); return

# Owner DM input handler
async def owner_dm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    mode = context.user_data.get("mode")
    if not mode: return

    msg = update.effective_message
    try:
        if mode == "set_interval":
            secs = parse_interval(msg.text or "")
            if secs < 10: raise ValueError("Interval too small (>=10s)")
            store["seconds"] = int(secs); save_store(); reschedule_job(context.application)
            await msg.reply_text(f"Interval set to {secs} sec ✅", reply_markup=MAIN_MENU)
            context.user_data.clear(); return

        if mode == "set_message":
            text = msg.text or ""; ents = msg.entities or []
            store["message"] = text
            store["entities"] = [_ent_to_dict(e) for e in ents]
            save_store()
            await msg.reply_text("Message updated ✅ (text + entities captured)", reply_markup=MAIN_MENU)
            context.user_data.clear(); return

        if mode == "set_photo":
            if msg.photo:
                store["photo"] = msg.photo[-1].file_id; save_store()
                await msg.reply_text("Photo updated ✅", reply_markup=MAIN_MENU)
            else:
                txt = (msg.text or "").strip().lower()
                if txt in {"none", "clear", "remove"}:
                    store["photo"] = None; save_store()
                    await msg.reply_text("Photo cleared ✅", reply_markup=MAIN_MENU)
                else:
                    await msg.reply_text("Please send an actual photo upload, or 'none' to clear.")
            context.user_data.clear(); return

        if mode == "set_buttons":
            raw = msg.text or ""
            parsed = parse_buttons_flexible(raw)
            if not parsed:
                raise ValueError("Couldn't parse buttons. Examples:\nOpen - example.com\nContact - @YourUser\nOr JSON: [[\"Open\",\"https://a.com\"]]")
            store["buttons"] = parsed; save_store()
            await msg.reply_text("Buttons updated ✅", reply_markup=MAIN_MENU)
            context.user_data.clear(); return

        if mode == "set_groups":
            lines = (msg.text or "").splitlines(); added, removed, errors = [], [], []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    removing = line.startswith("-")
                    ref = _normalize_chat_ref(line[1:] if removing else line)
                    gid = await _resolve_chat_id(context, ref)
                    if removing:
                        if gid in store["groups"]:
                            store["groups"].remove(gid); removed.append(gid)
                    else:
                        if gid not in store["groups"]:
                            store["groups"].append(gid); added.append(gid)
                except Exception as e:
                    errors.append(f"{line} → {e}")
            save_store()
            summary = ((f"Added: {len(added)}\n" if added else "") + (f"Removed: {len(removed)}\n" if removed else "") + ("Errors:\n"+"\n".join(errors) if errors else "")).strip() or "No changes"
            await msg.reply_text(summary, reply_markup=MAIN_MENU)
            context.user_data.clear(); return
    except Exception as e:
        await msg.reply_text(f"Error: {e}"); return

# Commands: /import, /preview, /forward, /entities
async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    src = update.message.reply_to_message
    if not src:
        await update.message.reply_text("Reply /import to the target message (with premium emoji)"); return

    # Try to capture original origin (supports 'forwarded from channel')
    orig_chat_id = None
    orig_msg_id = None

    fo = getattr(src, "forward_origin", None)
    if fo is not None:
        try:
            ch = getattr(fo, "chat", None)
            mid = getattr(fo, "message_id", None)
            if ch and getattr(ch, "id", None) and mid:
                orig_chat_id = ch.id
                orig_msg_id = mid
        except Exception:
            pass

    # Legacy fields for safety
    if (orig_chat_id is None or orig_msg_id is None) and hasattr(src, "forward_from_chat") and hasattr(src, "forward_from_message_id"):
        try:
            if src.forward_from_chat and src.forward_from_message_id:
                orig_chat_id = src.forward_from_chat.id
                orig_msg_id = src.forward_from_message_id
        except Exception:
            pass

    # Fallback: use the very message we replied to
    if orig_chat_id is None or orig_msg_id is None:
        orig_chat_id = src.chat_id
        orig_msg_id = src.message_id

    # Detect if the captured message already has inline keyboard
    has_kb = bool(getattr(src, "reply_markup", None) and getattr(src.reply_markup, "inline_keyboard", None))

    text = src.text or src.caption or ""
    ents = src.entities or src.caption_entities or []

    store["template"] = {"chat_id": orig_chat_id, "message_id": orig_msg_id}
    store["template_has_keyboard"] = bool(has_kb)
    store["message"] = text
    store["entities"] = [_ent_to_dict(e) for e in ents]
    if src.photo:
        store["photo"] = src.photo[-1].file_id
    save_store(); reschedule_job(context.application)
    kb_note = " (with inline buttons)" if has_kb else ""
    await update.message.reply_text(f"Imported ✅ template{kb_note}.")

async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    kb = build_keyboard(); tpl = store.get("template")
    if tpl and isinstance(tpl, dict) and tpl.get("chat_id") and tpl.get("message_id"):
        if store.get("use_forward"):
            fwd = await context.bot.forward_message(chat_id=update.effective_chat.id, from_chat_id=tpl["chat_id"], message_id=tpl["message_id"])
            if kb:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=INVISIBLE,
                    reply_markup=kb,
                    reply_parameters=ReplyParameters(message_id=fwd.message_id, allow_sending_without_reply=True, quote=False),
                )
        else:
            await context.bot.copy_message(chat_id=update.effective_chat.id, from_chat_id=tpl["chat_id"], message_id=tpl["message_id"], reply_markup=kb)
        return
    ent_objs = await _build_entities_from_store(); text = store.get("message", ""); photo = store.get("photo")
    if photo:
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photo, caption=text, reply_markup=kb, caption_entities=ent_objs if ent_objs else None)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=kb, entities=ent_objs if ent_objs else None)

async def cmd_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to a message in your DM with /forward to forward it to all groups, then place buttons under it."""
    if not is_owner(update): return
    src = update.message.reply_to_message
    if not src:
        await update.message.reply_text("Reply /forward to the target message you want to forward."); return
    kb = build_keyboard(); forwarded_count, fail = 0, []
    for gid in list(dict.fromkeys(store.get("groups", []))):
        try:
            fwd_msg = await context.bot.forward_message(chat_id=gid, from_chat_id=src.chat_id, message_id=src.message_id)
            forwarded_count += 1
            if kb:
                try:
                    await context.bot.send_message(
                        chat_id=gid,
                        text=INVISIBLE,
                        reply_markup=kb,
                        reply_parameters=ReplyParameters(message_id=fwd_msg.message_id, allow_sending_without_reply=True, quote=False),
                    )
                except Exception as e2:
                    print(f"[WARN] buttons failed for {gid}: {e2}")
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            try:
                fwd_msg = await context.bot.forward_message(chat_id=gid, from_chat_id=src.chat_id, message_id=src.message_id)
                forwarded_count += 1
                if kb:
                    try:
                        await context.bot.send_message(
                            chat_id=gid,
                            text=INVISIBLE,
                            reply_markup=kb,
                            reply_parameters=ReplyParameters(message_id=fwd_msg.message_id, allow_sending_without_reply=True, quote=False),
                        )
                    except Exception as e2:
                        print(f"[WARN] buttons failed after retry for {gid}: {e2}")
            except Exception as e2:
                fail.append((gid, str(e2)))
        except (TimedOut, NetworkError) as e:
            fail.append((gid, str(e)))
            await asyncio.sleep(1)
        except Exception as e:
            fail.append((gid, str(e)))
        await asyncio.sleep(0.5)
    summary = f"Forwarded to {forwarded_count} group(s)."
    if fail: summary += "\nFailed:\n" + "\n".join([f"{g}: {er}" for g, er in fail])
    await update.message.reply_text(summary)

async def cmd_entities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if context.args:
        raw = " ".join(context.args)
        try:
            ents = json.loads(raw)
            if not isinstance(ents, list): raise ValueError("JSON must be a list of MessageEntity dicts")
            store["entities"] = ents; save_store()
            await update.message.reply_text("Entities updated ✅")
        except Exception as e:
            await update.message.reply_text(f"Parse error: {e}")
    else:
        await update.message.reply_text(
            ("Send JSON (list of MessageEntity dicts) in the next message.\n"
             "Example: [{'type':'bold','offset':0,'length':5}, {'type':'custom_emoji','offset':6,'length':2,'custom_emoji_id':'538...'}]")
        )
        context.user_data["mode"] = "set_entities_json"

async def entities_followup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if context.user_data.get("mode") != "set_entities_json": return
    raw = update.effective_message.text or ""
    try:
        ents = json.loads(raw)
        if not isinstance(ents, list): raise ValueError("JSON must be a list")
        store["entities"] = ents; save_store()
        await update.effective_message.reply_text("Entities updated ✅", reply_markup=MAIN_MENU)
        context.user_data.clear()
    except Exception as e:
        await update.effective_message.reply_text(f"Parse error: {e}")

# ---------------- Inline Mode ----------------
async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Returns inline results that post the stored template (preserves premium emoji/entities)
    try:
        q = (update.inline_query.query or "").strip()
    except Exception:
        return
    kb = build_keyboard()
    text = store.get("message", "") or " "
    ent_objs = await _build_entities_from_store()

    results = [
        InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title="Send template with buttons",
            input_message_content=InputTextMessageContent(
                message_text=text,
                entities=ent_objs if ent_objs else None,
            ),
            reply_markup=kb,
            description="Imported text + premium emoji + your buttons",
        )
    ]
    if q:
        results.append(
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="Send typed text (quick)",
                input_message_content=InputTextMessageContent(message_text=q),
                reply_markup=kb,
                description="Use what you typed with same buttons",
            )
        )
    try:
        await update.inline_query.answer(results, cache_time=0, is_personal=True)
    except Exception as e:
        print(f"[WARN] inline answer failed: {e}")

# ---------------- Errors ----------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("[ERROR]", repr(context.error))

# ---------------- Main ----------------
async def on_startup(app: Application):
    # Ensure polling mode is clean
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    app.job_queue.run_repeating(_keepalive, interval=300, first=10, name="KEEPALIVE")
    reschedule_job(app)

def main():
    start_health_server()
    app = Application.builder().token(TOKEN).build()
    # Commands
    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler("entities", cmd_entities))
    app.add_handler(CommandHandler("import", cmd_import))
    app.add_handler(CommandHandler("preview", cmd_preview))
    app.add_handler(CommandHandler("forward", cmd_forward))
    app.add_handler(CommandHandler("attach", cmd_attach))
    app.add_handler(CommandHandler("detach", cmd_detach))
    app.add_handler(CommandHandler("mode", cmd_mode))
    # Menu callbacks
    app.add_handler(CallbackQueryHandler(on_menu_cb, pattern=r"^m:"))
    # Inline mode
    app.add_handler(InlineQueryHandler(on_inline))
    # Owner DM inputs
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.ALL, owner_dm_handler), group=1)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, entities_followup), group=0)
    # Errors
    app.add_error_handler(on_error)
    # Startup hook
    app.post_init = on_startup
    # Run
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"])  # minimal set

if __name__ == "__main__":
    main()
