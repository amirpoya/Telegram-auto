#!/usr/bin/env python3
import json, os, re, asyncio
from typing import List, Tuple, Dict, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ChatMemberHandler, MessageHandler, filters, JobQueue
)

# ---- Health HTTP server so Render sees an open port ----
import threading, http.server, socketserver
def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a, **k): return
    def serve():
        with socketserver.TCPServer(("", port), Handler) as httpd:
            httpd.allow_reuse_address = True
            httpd.serve_forever()
    threading.Thread(target=serve, daemon=True).start()
# --------------------------------------------------------

# ---- Self keepalive: ping PUBLIC_URL every 4 minutes ----
import aiohttp
async def _keepalive(context: ContextTypes.DEFAULT_TYPE):
    url = os.getenv("PUBLIC_URL", "").strip()
    if not url: return
    try:
        tout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=tout) as s:
            async with s.get(url) as r:
                await r.read()
    except Exception:
        pass
# --------------------------------------------------------

# Load credentials
TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_IDS_ENV = os.getenv("OWNER_IDS", "").strip()
OWNER_IDS = {int(x) for x in OWNER_IDS_ENV.split(",") if x.strip().isdigit()}
if not TOKEN or not OWNER_IDS:
    raise SystemExit("BOT_TOKEN and OWNER_IDS env vars are required. Example OWNER_IDS='123,456'")

DATA_FILE = "global_settings.json"
DEFAULTS = {
    "message": "Hello! Scheduled message 🌟",
    "seconds": 15 * 60,
    "enabled": False,
    "groups": [],
    "buttons": [],     # [["SHOP BOT","https://t.me/YourShopBot"], ...]
    "photo": None,     # URL or file_id
    "entities": []     # Telegram entities for formatting (bold, italic, link, quote, ...)
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
    if not isinstance(data.get("groups"), list): data["groups"] = []
    if not isinstance(data.get("buttons"), list): data["buttons"] = []
    if not isinstance(data.get("entities"), list): data["entities"] = []
    return data

store: Dict[str, Any] = load_store()

def save_store():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)

def is_owner(update: Update) -> bool:
    return update.effective_chat.type == "private" and (update.effective_user and update.effective_user.id in OWNER_IDS)

def parse_interval(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("m"): return int(float(s[:-1]) * 60)
    if s.endswith("h"): return int(float(s[:-1]) * 3600)
    if s.endswith("d"): return int(float(s[:-1]) * 86400)
    if re.fullmatch(r"\d+", s): return int(s)
    raise ValueError("Invalid interval. Example: 15m or 2h or 1d or raw seconds")

def build_keyboard() -> InlineKeyboardMarkup | None:
    btns: List[List[str]] = store.get("buttons", [])
    if not btns: return None
    rows = [[InlineKeyboardButton(text=label, url=url)] for label, url in btns]
    return InlineKeyboardMarkup(rows)

async def send_to_all_groups(context: ContextTypes.DEFAULT_TYPE):
    if not store["enabled"]:
        return
    msg_text = store["message"]
    photo = store.get("photo")
    kb = build_keyboard()

    # restore entities
    ent_objs = [MessageEntity(type=d["type"], offset=d["offset"], length=d["length"],
                              url=d.get("url"), language=d.get("language"))
                for d in store.get("entities", [])]

    for gid in list(set(store["groups"])):
        try:
            if photo:
                await context.bot.send_photo(chat_id=gid, photo=photo, caption=msg_text,
                                             reply_markup=kb, caption_entities=ent_objs)
            else:
                await context.bot.send_message(chat_id=gid, text=msg_text,
                                               reply_markup=kb, entities=ent_objs)
        except Exception:
            # if failed (kicked/no permission) drop the group
            if gid in store["groups"]:
                store["groups"].remove(gid); save_store()

def reschedule_job(app: Application):
    for j in app.job_queue.get_jobs_by_name("GLOBAL_POSTER"):
        j.schedule_removal()
    if store["enabled"]:
        app.job_queue.run_repeating(send_to_all_groups, interval=store["seconds"], first=0, name="GLOBAL_POSTER")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    if not is_owner(update):
        return await update.message.reply_text("Hi! Only the bot owners can change settings.")
    await update.message.reply_text(
        "Owner controls:\n"
        "/enable — turn global posting ON\n"
        "/disable — turn it OFF\n"
        "/set_message <text>\n"
        "/set_interval <15m|2h|1d|secs>\n"
        "/set_photo <url|file_id|none>\n"
        "/set_buttons Label|https://a;Label2|https://b\n"
        "/add_button Label|https://...\n"
        "/clear_buttons\n"
        "/status"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    mins = store["seconds"] // 60
    btns = "\n".join([f"- {l} → {u}" for l, u in store["buttons"]]) or "-"
    await update.message.reply_text(
        f"Status: {'Enabled' if store['enabled'] else 'Disabled'}\n"
        f"Interval: {store['seconds']} seconds (~{mins} minutes)\n"
        f"Photo: {store['photo'] or '-'}\n"
        f"Message:\n{store['message']}\n\n"
        f"Buttons:\n{btns}\n\n"
        f"Groups count: {len(store['groups'])}"
    )

async def cmd_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    store["enabled"] = True; save_store()
    reschedule_job(context.application)
    await update.message.reply_text("Global auto-posting enabled ✅")

async def cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    store["enabled"] = False; save_store()
    reschedule_job(context.application)
    await update.message.reply_text("Global auto-posting disabled ⏹️")

# keep full formatting from Telegram message (no HTML typing needed)
async def cmd_set_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    msg = update.effective_message
    raw = msg.text or ""
    space_idx = raw.find(" ")
    text = raw[space_idx + 1:] if space_idx != -1 else ""
    if not text.strip():
        return await msg.reply_text("Usage: /set_message Your text (supports Telegram formatting)")

    ents = []
    if msg.entities:
        start = space_idx + 1 if space_idx != -1 else len(raw)
        for e in msg.entities:
            if e.type == "bot_command": continue
            if e.offset + e.length <= start: continue
            new_offset = max(0, e.offset - start)
            new_length = e.length - max(0, start - e.offset)
            if new_length > 0:
                d = {"type": e.type, "offset": new_offset, "length": new_length}
                if e.url: d["url"] = e.url
                if e.language: d["language"] = e.language
                ents.append(d)

    store["message"] = text
    store["entities"] = ents
    save_store()
    await msg.reply_text("Global message updated ✍️ (formatting preserved)")

async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if not context.args:
        return await update.message.reply_text("Usage: /set_interval 15m")
    try:
        seconds = parse_interval(context.args[0])
        if seconds < 60: return await update.message.reply_text("Minimum interval is 60 seconds.")
    except ValueError as e:
        return await update.message.reply_text(str(e))
    store["seconds"] = seconds; save_store()
    reschedule_job(context.application)
    await update.message.reply_text("Global interval updated ⏱️")

async def cmd_set_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if not context.args:
        return await update.message.reply_text("Usage: /set_photo <url|file_id|none>")
    arg = context.args[0].strip()
    store["photo"] = None if arg.lower() == "none" else arg
    save_store()
    await update.message.reply_text("Photo setting updated 🖼️")

def parse_buttons_arg(s: str) -> List[Tuple[str, str]]:
    pairs = []
    for chunk in [c.strip() for c in s.split(";") if c.strip()]:
        if "|" not in chunk: raise ValueError("Bad format")
        label, url = [x.strip() for x in chunk.split("|", 1)]
        if not (label and url and (url.startswith("http://") or url.startswith("https://"))):
            raise ValueError("Each pair must be: Label|https://...")
        pairs.append((label, url))
    return pairs

async def cmd_set_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if not context.args:
        return await update.message.reply_text("Usage: /set_buttons Label1|https://a;Label2|https://b")
    try:
        pairs = parse_buttons_arg(" ".join(context.args))
    except ValueError:
        return await update.message.reply_text(
            "Invalid format. Example:\n/set_buttons Shop|https://t.me/YourBot; Group|https://t.me/YourGroup"
        )
    store["buttons"] = [list(p) for p in pairs][:8]
    save_store()
    await update.message.reply_text("Buttons updated 🔘")

async def cmd_add_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if not context.args:
        return await update.message.reply_text("Usage: /add_button Label|https://example.com")
    try:
        label, url = parse_buttons_arg(" ".join(context.args))[0]
    except Exception:
        return await update.message.reply_text("Invalid format. Use: Label|https://...")
    if len(store["buttons"]) >= 8:
        return await update.message.reply_text("Max 8 buttons allowed.")
    store["buttons"].append([label, url]); save_store()
    await update.message.reply_text("Button added ➕")

async def cmd_clear_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    store["buttons"] = []; save_store()
    await update.message.reply_text("All buttons cleared ❌")

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    status = update.my_chat_member.new_chat_member.status
    if status in ("member", "administrator", "restricted"):
        if chat.id not in store["groups"]:
            store["groups"].append(chat.id); save_store()
    else:
        if chat.id in store["groups"]:
            store["groups"].remove(chat.id); save_store()

if __name__ == "__main__":
    start_health_server()  # port open for Render

    jq = JobQueue()
    app = Application.builder().token(TOKEN).job_queue(jq).build()
    jq.set_application(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("enable", cmd_enable))
    app.add_handler(CommandHandler("disable", cmd_disable))
    app.add_handler(CommandHandler("set_message", cmd_set_message))
    app.add_handler(CommandHandler("set_interval", cmd_set_interval))
    app.add_handler(CommandHandler("set_photo", cmd_set_photo))
    app.add_handler(CommandHandler("set_buttons", cmd_set_buttons))
    app.add_handler(CommandHandler("add_button", cmd_add_button))
    app.add_handler(CommandHandler("clear_buttons", cmd_clear_buttons))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.GROUPS, lambda u, c: None))

    # schedule poster + internal keepalive
    reschedule_job(app)
    app.job_queue.run_repeating(_keepalive, interval=240, first=10, name="KEEPALIVE")

    print("Bot is running on Render…")
    app.run_polling()
