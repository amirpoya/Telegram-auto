#!/usr/bin/env python3
import os, re, json
from typing import List, Tuple, Dict, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    ChatMemberHandler, filters, JobQueue
)

# Load credentials from env
TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").strip().split(",") if x.strip().isdigit()}
if not TOKEN or not OWNER_IDS:
    raise SystemExit("BOT_TOKEN and OWNER_IDS required.")

DATA_FILE = "global_settings.json"
DEFAULTS = {
    "message": "Hello! Scheduled message 🌟",
    "seconds": 15 * 60,
    "enabled": False,
    "buttons": [],
    "photo": None
}

# Load and save settings
def load_store() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except:
            data = {}
    else:
        data = {}
    for k, v in DEFAULTS.items():
        data.setdefault(k, v)
    return data

store = load_store()
def save_store():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)

def is_owner(update: Update) -> bool:
    return update.effective_chat.type == "private" and update.effective_user.id in OWNER_IDS

def parse_interval(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("m"): return int(float(s[:-1]) * 60)
    if s.endswith("h"): return int(float(s[:-1]) * 3600)
    if s.endswith("d"): return int(float(s[:-1]) * 86400)
    if re.fullmatch(r"\d+", s): return int(s)
    raise ValueError("Invalid interval. Example: 15m or 2h or 1d")

def build_keyboard() -> InlineKeyboardMarkup | None:
    btns: List[List[str]] = store.get("buttons", [])
    if not btns:
        return None
    rows = [[InlineKeyboardButton(text=label, url=url)] for label, url in btns]
    return InlineKeyboardMarkup(rows)

async def get_all_groups(app: Application) -> List[int]:
    groups = []
    async for dialog in app.bot.get_updates():
        pass
    async for chat in app.bot.get_my_commands(): # Placeholder to keep API awake
        pass
    updates = await app.bot.get_updates()
    for update in updates:
        chat = getattr(update.message, "chat", None)
        if chat and chat.type in ("group", "supergroup"):
            groups.append(chat.id)
    return list(set(groups))

async def send_to_all_groups(context: ContextTypes.DEFAULT_TYPE):
    if not store["enabled"]:
        return
    kb = build_keyboard()
    msg = store["message"]
    photo = store.get("photo")
    app = context.application

    groups = await get_all_groups(app)
    for gid in groups:
        try:
            if photo:
                await context.bot.send_photo(chat_id=gid, photo=photo, caption=msg, reply_markup=kb)
            else:
                await context.bot.send_message(chat_id=gid, text=msg, reply_markup=kb)
        except Exception as e:
            print(f"Failed to send to {gid}: {e}")

def reschedule_job(app: Application):
    for j in app.job_queue.get_jobs_by_name("GLOBAL_POSTER"):
        j.schedule_removal()
    if store["enabled"]:
        app.job_queue.run_repeating(send_to_all_groups, interval=store["seconds"], first=0, name="GLOBAL_POSTER")

# Commands
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    await update.message.reply_text(
        "/enable\n/disable\n/set_message <text>\n/set_interval <15m|2h|1d|secs>\n"
        "/set_photo <url|file_id|none>\n/set_buttons Label|https://a;Label2|https://b\n"
        "/add_button Label|https://...\n/clear_buttons\n/status"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    mins = store["seconds"] // 60
    btns = "\n".join([f"- {l} → {u}" for l, u in store["buttons"]]) or "-"
    await update.message.reply_text(
        f"Status: {'Enabled' if store['enabled'] else 'Disabled'}\n"
        f"Interval: {store['seconds']} sec (~{mins} min)\n"
        f"Photo: {store['photo'] or '-'}\nMessage:\n{store['message']}\nButtons:\n{btns}"
    )

async def cmd_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    store["enabled"] = True
    save_store()
    reschedule_job(context.application)
    await update.message.reply_text("Enabled ✅")

async def cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    store["enabled"] = False
    save_store()
    reschedule_job(context.application)
    await update.message.reply_text("Disabled ⏹️")

async def cmd_set_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    text = " ".join(context.args)
    if not text: return await update.message.reply_text("Usage: /set_message Hello")
    store["message"] = text
    save_store()
    await update.message.reply_text("Message updated")

async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    try:
        seconds = parse_interval(context.args[0])
        if seconds < 60:
            return await update.message.reply_text("Min interval is 60s")
    except:
        return await update.message.reply_text("Invalid format")
    store["seconds"] = seconds
    save_store()
    reschedule_job(context.application)
    await update.message.reply_text("Interval updated")

async def cmd_set_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    arg = context.args[0].strip()
    store["photo"] = None if arg.lower() == "none" else arg
    save_store()
    await update.message.reply_text("Photo updated")

def parse_buttons_arg(s: str) -> List[Tuple[str, str]]:
    pairs = []
    for chunk in s.split(";"):
        if "|" not in chunk: continue
        label, url = chunk.split("|", 1)
        pairs.append((label.strip(), url.strip()))
    return pairs

async def cmd_set_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    pairs = parse_buttons_arg(" ".join(context.args))
    store["buttons"] = [list(p) for p in pairs][:8]
    save_store()
    await update.message.reply_text("Buttons updated")

async def cmd_add_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    pairs = parse_buttons_arg(" ".join(context.args))
    if pairs:
        store["buttons"].append(list(pairs[0]))
        save_store()
        await update.message.reply_text("Button added")

async def cmd_clear_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    store["buttons"] = []
    save_store()
    await update.message.reply_text("Buttons cleared")

if __name__ == "__main__":
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
    app.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.GROUPS, lambda u, c: None))

    reschedule_job(app)
    print("Bot is running...")
    app.run_polling()
