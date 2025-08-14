#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Global Poster Bot — Fixed & Hardened (with Premium Emoji + entity capture)
- python-telegram-bot (v20+)
- Render-compatible health server (binds $PORT)
- Async-safe handlers, robust JobQueue scheduling
- Immutable MessageEntity handling with custom_emoji_id support
- Inline menu to manage: Enable/Disable, Interval, Message, Photo, Buttons, Groups
- Self-ping keepalive (optional via PUBLIC_URL)
- NEW: When you set the Message from the menu, the bot now CAPTURES your entities
       (including premium emojis) automatically from the text you send.

ENV:
  BOT_TOKEN   = ... (required)
  OWNER_IDS   = 123,456 (comma separated user IDs; required)
  PUBLIC_URL  = https://telegram-auto.onrender.com (optional for keepalive)

FILES:
  global_settings.json  (auto-created)
"""

from __future__ import annotations
import os, re, json, asyncio, threading, http.server, socketserver
from typing import List, Tuple, Dict, Any, Union
from urllib.parse import urlparse

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
)
from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    CallbackQueryHandler, filters, JobQueue
)
import aiohttp

# ---------------- Health server (Render needs an open port) ----------------

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


# ---------------- Self-ping (prevent Render inactivity sleep) --------------

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


# ---------------- Credentials ----------------------------------------------

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").strip().split(",") if x.strip().isdigit()}
if not TOKEN or not OWNER_IDS:
    raise SystemExit("BOT_TOKEN and OWNER_IDS env vars are required. Example OWNER_IDS='123,456'")


# ---------------- Storage ---------------------------------------------------

DATA_FILE = "global_settings.json"
DEFAULTS: Dict[str, Any] = {
    "message": "Hello! Scheduled message 🌟",
    "seconds": 15 * 60,
    "enabled": False,
    "groups": [],           # list[int]
    "buttons": [],          # list[[label, url]]
    "photo": None,          # file_id or url
    "entities": []          # list[dict]
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
    return data


store: Dict[str, Any] = load_store()


def save_store() -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


# ---------------- Helpers ---------------------------------------------------

def is_owner(update: Update) -> bool:
    return (
        update.effective_chat
        and update.effective_chat.type == "private"
        and update.effective_user
        and update.effective_user.id in OWNER_IDS
    )


def parse_interval(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("m"):
        return int(float(s[:-1]) * 60)
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    if s.endswith("d"):
        return int(float(s[:-1]) * 86400)
    if re.fullmatch(r"\d+", s):
        return int(s)
    raise ValueError("Invalid interval. Example: 15m or 2h or 1d or raw seconds")


def build_keyboard() -> InlineKeyboardMarkup | None:
    btns: List[List[str]] = store.get("buttons", [])
    if not btns:
        return None
    rows = [[InlineKeyboardButton(text=label, url=url)] for label, url in btns]
    return InlineKeyboardMarkup(rows)


def _normalize_chat_ref(ref: str) -> Union[int, str]:
    ref = ref.strip()
    if not ref:
        raise ValueError("Empty reference.")
    if re.fullmatch(r"-?\d{6,}", ref):
        return int(ref)
    if ref.startswith("@"):
        return ref
    if ref.startswith("http://") or ref.startswith("https://"):
        u = urlparse(ref)
        if u.netloc.lower() != "t.me":
            raise ValueError("Only t.me links are supported.")
        parts = [p for p in u.path.split("/") if p]
        if not parts:
            raise ValueError("Bad t.me link.")
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
    if isinstance(ref, int):
        return ref
    chat = await context.bot.get_chat(ref)
    return chat.id


# --- helpers: serialize entities (keeps custom_emoji_id) ---

def _ent_to_dict(e: MessageEntity) -> dict:
    return {
        "type": e.type,
        "offset": e.offset,
        "length": e.length,
        "url": getattr(e, "url", None),
        "language": getattr(e, "language", None),
        "custom_emoji_id": getattr(e, "custom_emoji_id", None),
    }

# --- /import : reply to a message to import its text/caption + entities (+photo) ---
async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    src = update.message.reply_to_message
    if not src:
        await update.message.reply_text("Reply /import to the target message (with premium emoji)")
        return

    text = src.text or src.caption or ""
    ents = src.entities or src.caption_entities or []

    store["message"] = text
    store["entities"] = [_ent_to_dict(e) for e in ents]

    if src.photo:
        store["photo"] = src.photo[-1].file_id

    save_store()
    reschedule_job(context.application)
    await update.message.reply_text("Imported ✅ (message, entities, photo if any)")


# ---------------- Broadcaster (entities support + throttling) --------------

async def _build_entities_from_store() -> List[MessageEntity]:
    ent_objs: List[MessageEntity] = []
    for d in store.get("entities", []):
        # MessageEntity in PTB v20+ is immutable; pass all fields in constructor
        ent = MessageEntity(
            type=d.get("type"),
            offset=d.get("offset", 0),
            length=d.get("length", 0),
            url=d.get("url"),
            language=d.get("language"),
            custom_emoji_id=d.get("custom_emoji_id"),  # only valid for type=="custom_emoji"
            user=None,
        )
        ent_objs.append(ent)
    return ent_objs


async def send_to_all_groups(context: ContextTypes.DEFAULT_TYPE):
    if not store.get("enabled"):
        return

    msg_text: str = store.get("message", "")
    photo = store.get("photo")
    kb = build_keyboard()
    ent_objs = await _build_entities_from_store()

    # iterate unique group ids (order preserved)
    for gid in list(dict.fromkeys(store.get("groups", []))):
        try:
            if photo:
                await context.bot.send_photo(
                    chat_id=gid,
                    photo=photo,
                    caption=msg_text,
                    reply_markup=kb,
                    caption_entities=ent_objs if ent_objs else None,
                )
            else:
                await context.bot.send_message(
                    chat_id=gid,
                    text=msg_text,
                    reply_markup=kb,
                    entities=ent_objs if ent_objs else None,
                )
        except RetryAfter as e:
            print(f"[WARN] Rate limited for group {gid}, waiting {e.retry_after}s")
            await asyncio.sleep(int(e.retry_after) + 1)
            try:
                if photo:
                    await context.bot.send_photo(
                        chat_id=gid,
                        photo=photo,
                        caption=msg_text,
                        reply_markup=kb,
                        caption_entities=ent_objs if ent_objs else None,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=gid,
                        text=msg_text,
                        reply_markup=kb,
                        entities=ent_objs if ent_objs else None,
                    )
            except Exception as e2:
                print(f"[WARN] retry failed for {gid}: {e2}")
        except (TimedOut, NetworkError) as e:
            print(f"[WARN] Network error for group {gid}: {e}")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"[WARN] send failed for {gid}: {e}")
        await asyncio.sleep(0.5)  # small delay for reliability


def reschedule_job(app: Application):
    # ensure single repeating job (but allow overlap via job_kwargs if desired)
    for j in app.job_queue.get_jobs_by_name("GLOBAL_POSTER"):
        j.schedule_removal()

    if store.get("enabled"):
        app.job_queue.run_repeating(
            send_to_all_groups,
            interval=store.get("seconds", 900),
            first=0,
            name="GLOBAL_POSTER",
            job_kwargs={
                "max_instances": 1,       # change to 2+ if you want overlap
                "coalesce": True,
                "misfire_grace_time": 60,
            },
        )


# ---------------- Menu & Interactive UX ------------------------------------

MAIN_MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("⚡ Status", callback_data="m:status"),
        InlineKeyboardButton("✅ Enable", callback_data="m:enable"),
        InlineKeyboardButton("⏹️ Disable", callback_data="m:disable"),
    ],
    [
        InlineKeyboardButton("⏰ Interval", callback_data="m:interval"),
        InlineKeyboardButton("✍️ Message", callback_data="m:message"),
    ],
    [
        InlineKeyboardButton("🖼️ Photo", callback_data="m:photo"),
        InlineKeyboardButton("🔘 Buttons", callback_data="m:buttons"),
    ],
    [
        InlineKeyboardButton("👥 Groups", callback_data="m:groups"),
        InlineKeyboardButton("❓ Help", callback_data="m:help"),
    ],
])


def back_menu_kb():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Back to Menu", callback_data="m:menu")]]
    )


def status_text() -> str:
    mins = store.get("seconds", 0) // 60
    btns = "\n".join([f"▫️ {l} → {u}" for l, u in store.get("buttons", [])]) or "-"
    return (
        f"✨ <b>Status:</b> {'Enabled ✅' if store.get('enabled') else 'Disabled ⏹️'}\n"
        f"⏰ Interval: <code>{store.get('seconds')}</code> sec (~{mins} min)\n"
        f"🖼️ Photo: <code>{store.get('photo') or 'None'}</code>\n"
        f"✍️ Message:\n<code>{(store.get('message') or '').replace('<','&lt;').replace('>','&gt;')}</code>\n"
        f"\n🔘 Buttons:\n{btns}\n"
        f"\n👥 Groups count: <b>{len(store.get('groups', []))}</b>"
    )


# ---------------- Owner-only Commands --------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_owner(update):
        await update.message.reply_text("Hi! Only bot owners can change settings.")
        return
    await update.message.reply_text(
        "🌟 Bot Management Menu:", reply_markup=MAIN_MENU, parse_mode="HTML"
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "🌟 Bot Management Menu:", reply_markup=MAIN_MENU, parse_mode="HTML"
    )


# ---------------- Callback Query (Menu) ------------------------------------

async def on_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (update.effective_user and update.effective_user.id in OWNER_IDS):
        return

    q = update.callback_query
    data = (q.data or "").strip()
    context.user_data.clear()

    try:
        await q.answer()
    except Exception:
        pass

    async def safe_edit(text: str, **kwargs):
        try:
            return await q.edit_message_text(text, **kwargs)
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            raise

    if data == "m:status":
        await safe_edit(status_text(), reply_markup=MAIN_MENU, parse_mode="HTML")
        return

    if data == "m:enable":
        store["enabled"] = True
        save_store()
        reschedule_job(context.application)
        await safe_edit("Auto-posting enabled ✅", reply_markup=MAIN_MENU)
        return

    if data == "m:disable":
        store["enabled"] = False
        save_store()
        reschedule_job(context.application)
        await safe_edit("Auto-posting disabled ⏹️", reply_markup=MAIN_MENU)
        return

    if data == "m:interval":
        context.user_data["mode"] = "set_interval"
        await safe_edit(
            "Send new interval. Examples: <code>900</code> (sec) / <code>15m</code> / <code>2h</code>",
            reply_markup=back_menu_kb(),
            parse_mode="HTML",
        )
        return

    if data == "m:message":
        context.user_data["mode"] = "set_message"
        await safe_edit(
            "Send the new message text. Any formatting / premium emojis you apply will be captured automatically.",
            reply_markup=back_menu_kb(),
        )
        return

    if data == "m:photo":
        context.user_data["mode"] = "set_photo"
        await safe_edit(
            "Send a photo (as photo upload) to set. Send 'none' to clear.",
            reply_markup=back_menu_kb(),
        )
        return

    if data == "m:buttons":
        context.user_data["mode"] = "set_buttons"
        await safe_edit(
            (
                "Send buttons list as JSON array of [label, url].\n"
                "Example: [[\"Open\", \"https://example.com\"],[\"Docs\", \"https://docs\"]]"
            ),
            reply_markup=back_menu_kb(),
        )
        return

    if data == "m:groups":
        context.user_data["mode"] = "set_groups"
        await safe_edit(
            (
                "Send chat references (one per line). Examples: -100123..., @publicname, or t.me/c/<id>.\n"
                "Use prefix '-' to remove."
            ),
            reply_markup=back_menu_kb(),
        )
        return

    if data == "m:help":
        await safe_edit(
            (
                "<b>Help</b>\n\n"
                "- Use the menu to configure interval/message/photo/buttons/groups.\n"
                "- Entities are captured when you set the Message, or via /entities command.\n"
                "- Premium emoji: appears as type='custom_emoji' with its custom_emoji_id.\n"
                "- JobQueue skips mean previous run still executing; increase interval or allow overlap.\n"
            ),
            reply_markup=MAIN_MENU,
            parse_mode="HTML",
        )
        return

    if data == "m:menu":
        await safe_edit("🌟 Bot Management Menu:", reply_markup=MAIN_MENU)
        return


# ---------------- Text/Photo Input Handler (Owner DM) ----------------------

async def owner_dm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    mode = context.user_data.get("mode")
    if not mode:
        return

    msg = update.effective_message

    try:
        if mode == "set_interval":
            secs = parse_interval(msg.text or "")
            if secs < 10:
                raise ValueError("Interval too small (>=10s)")
            store["seconds"] = int(secs)
            save_store()
            reschedule_job(context.application)
            await msg.reply_text(f"Interval set to {secs} sec ✅", reply_markup=MAIN_MENU)
            context.user_data.clear()
            return

        if mode == "set_message":
            # Capture plain text + ENTITIES from the message you sent (includes premium emojis)
            text = msg.text or ""
            ents = msg.entities or []
            store["message"] = text
            store["entities"] = [_ent_to_dict(e) for e in ents]
            save_store()
            await msg.reply_text("Message updated ✅ (text + entities captured)", reply_markup=MAIN_MENU)
            context.user_data.clear()
            return

        if mode == "set_photo":
            if msg.photo:
                file_id = msg.photo[-1].file_id
                store["photo"] = file_id
                save_store()
                await msg.reply_text("Photo updated ✅", reply_markup=MAIN_MENU)
            else:
                txt = (msg.text or "").strip().lower()
                if txt in {"none", "clear", "remove"}:
                    store["photo"] = None
                    save_store()
                    await msg.reply_text("Photo cleared ✅", reply_markup=MAIN_MENU)
                else:
                    await msg.reply_text("Please send an actual photo upload, or 'none' to clear.")
            context.user_data.clear()
            return

        if mode == "set_buttons":
            raw = msg.text or ""
            btns = json.loads(raw)
            if not isinstance(btns, list):
                raise ValueError("JSON must be a list of [label, url]")
            norm: List[List[str]] = []
            for item in btns:
                if not (isinstance(item, list) and len(item) == 2):
                    raise ValueError("Each item must be [label, url]")
                norm.append([str(item[0]), str(item[1])])
            store["buttons"] = norm
            save_store()
            await msg.reply_text("Buttons updated ✅", reply_markup=MAIN_MENU)
            context.user_data.clear()
            return

        if mode == "set_groups":
            lines = (msg.text or "").splitlines()
            added, removed, errors = [], [], []
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
                            store["groups"].remove(gid)
                            removed.append(gid)
                    else:
                        if gid not in store["groups"]:
                            store["groups"].append(gid)
                            added.append(gid)
                except Exception as e:
                    errors.append(f"{line} → {e}")
            save_store()
            summary = (
                (f"Added: {len(added)}\n" if added else "")
                + (f"Removed: {len(removed)}\n" if removed else "")
                + ("Errors:\n" + "\n".join(errors) if errors else "")
            ).strip() or "No changes"
            await msg.reply_text(summary, reply_markup=MAIN_MENU)
            context.user_data.clear()
            return

    except Exception as e:
        await msg.reply_text(f"Error: {e}")
        return


# ---------------- Optional: /entities command ------------------------------

async def cmd_entities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    # Expect a JSON after command or in next message if empty
    if context.args:
        raw = " ".join(context.args)
        try:
            ents = json.loads(raw)
            if not isinstance(ents, list):
                raise ValueError("JSON must be a list of MessageEntity dicts")
            store["entities"] = ents
            save_store()
            await update.message.reply_text("Entities updated ✅")
        except Exception as e:
            await update.message.reply_text(f"Parse error: {e}")
    else:
        await update.message.reply_text(
            (
                "Send JSON (list of MessageEntity dicts) in the next message.\n"
                "Minimal example:\n"
                "[{'type':'bold','offset':0,'length':5}, {'type':'custom_emoji','offset':6,'length':2,'custom_emoji_id':'538...'}]"
            )
        )
        context.user_data["mode"] = "set_entities_json"


async def entities_followup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if context.user_data.get("mode") != "set_entities_json":
        return
    raw = update.effective_message.text or ""
    try:
        ents = json.loads(raw)
        if not isinstance(ents, list):
            raise ValueError("JSON must be a list")
        store["entities"] = ents
        save_store()
        await update.effective_message.reply_text("Entities updated ✅", reply_markup=MAIN_MENU)
        context.user_data.clear()
    except Exception as e:
        await update.effective_message.reply_text(f"Parse error: {e}")


# ---------------- Error Handler --------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("[ERROR]", repr(context.error))


# ---------------- Main ------------------------------------------------------

async def on_startup(app: Application):
    # schedule keepalive ping every 5 minutes
    app.job_queue.run_repeating(_keepalive, interval=300, first=10, name="KEEPALIVE")
    # schedule broadcaster according to current settings
    reschedule_job(app)


def main():
    start_health_server()

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler("entities", cmd_entities))
    app.add_handler(CommandHandler("import", cmd_import))

    # Menu callbacks
    app.add_handler(CallbackQueryHandler(on_menu_cb, pattern=r"^m:"))

    # Owner DM inputs (text & photos)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.ALL, owner_dm_handler), group=1)
    # entities followup has to run before generic owner handler when in that mode
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, entities_followup), group=0)

    # Errors
    app.add_error_handler(on_error)

    # Startup hooks
    app.post_init = on_startup  # PTB automatically awaits this coroutine on start

    # Run (polling is simplest and Render-friendly since we keep a health port open)
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"])  # minimal set


if __name__ == "__main__":
    main()
