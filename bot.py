#!/usr/bin/env python3
import os, re, json, asyncio, threading, http.server, socketserver
from typing import List, Tuple, Dict, Any, Union
from urllib.parse import urlparse

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
)
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    ChatMemberHandler, CallbackQueryHandler, filters, JobQueue
)
import aiohttp

# ---------------- Health server (Render needs an open port) ----------------
def start_health_server():
    port = int(os.environ["PORT"])
    print("Health server binding on PORT =", port, flush=True)
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a, **k): return
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
DEFAULTS = {
    "message": "Hello! Scheduled message 🌟",
    "seconds": 15 * 60,
    "enabled": False,
    "groups": [],
    "buttons": [],
    "photo": None,
    "entities": []
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

# ---------------- Helpers ---------------------------------------------------
def is_owner(update: Update) -> bool:
    return (update.effective_chat and update.effective_chat.type == "private" and
            update.effective_user and update.effective_user.id in OWNER_IDS)

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

# ---------------- Broadcaster (entities support + throttling) --------------
async def send_to_all_groups(context: ContextTypes.DEFAULT_TYPE):
    if not store["enabled"]:
        return
    msg_text = store["message"]
    photo = store.get("photo")
    kb = build_keyboard()
    ent_objs = [MessageEntity(type=d["type"], offset=d["offset"], length=d["length"],
                              url=d.get("url"), language=d.get("language"))
                for d in store.get("entities", [])]

    for gid in list(dict.fromkeys(store["groups"])):
        try:
            if photo:
                await context.bot.send_photo(
                    chat_id=gid, photo=photo, caption=msg_text,
                    reply_markup=kb, caption_entities=ent_objs
                )
            else:
                await context.bot.send_message(
                    chat_id=gid, text=msg_text,
                    reply_markup=kb, entities=ent_objs
                )
            await asyncio.sleep(0.05)  # tiny gap
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                if photo:
                    await context.bot.send_photo(
                        chat_id=gid, photo=photo, caption=msg_text,
                        reply_markup=kb, caption_entities=ent_objs
                    )
                else:
                    await context.bot.send_message(
                        chat_id=gid, text=msg_text,
                        reply_markup=kb, entities=ent_objs
                    )
            except Exception as e2:
                print(f"[WARN] send retry failed for {gid}: {e2}")
        except (TimedOut, NetworkError):
            await asyncio.sleep(1)
        except Exception as e:
            print(f"[WARN] send failed for {gid}: {e}")

def reschedule_job(app: Application):
    for j in app.job_queue.get_jobs_by_name("GLOBAL_POSTER"):
        j.schedule_removal()
    if store["enabled"]:
        app.job_queue.run_repeating(send_to_all_groups, interval=store["seconds"], first=0, name="GLOBAL_POSTER")

# ---------------- Menu & Interactive UX -------------------------------------
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("⚡ Status", callback_data="m:status"),
     InlineKeyboardButton("✅ Enable", callback_data="m:enable"),
     InlineKeyboardButton("⏹️ Disable", callback_data="m:disable")],
    [InlineKeyboardButton("⏰ Interval", callback_data="m:interval"),
     InlineKeyboardButton("✍️ Message", callback_data="m:message")],
    [InlineKeyboardButton("🖼️ Photo", callback_data="m:photo"),
     InlineKeyboardButton("🔘 Buttons", callback_data="m:buttons")],
    [InlineKeyboardButton("👥 Groups", callback_data="m:groups"),
     InlineKeyboardButton("❓ Help", callback_data="m:help")]
])

def status_text():
    mins = store["seconds"] // 60
    btns = "\n".join([f"▫️ {l} → {u}" for l, u in store["buttons"]]) or "-"
    return (
        f"✨ <b>Status:</b> {'Enabled ✅' if store['enabled'] else 'Disabled ⏹️'}\n"
        f"⏰ Interval: <code>{store['seconds']}</code> sec (~{mins} min)\n"
        f"🖼️ Photo: <code>{store['photo'] or 'None'}</code>\n"
        f"✍️ Message:\n<code>{store['message']}</code>\n"
        f"\n🔘 Buttons:\n{btns}\n"
        f"\n👥 Groups count: <b>{len(store['groups'])}</b>"
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_owner(update):
        return await update.message.reply_text("Hi! Only bot owners can change settings.")
    await update.message.reply_text("🌟 Bot Management Menu:", reply_markup=MAIN_MENU, parse_mode="HTML")

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    await update.message.reply_text("🌟 Bot Management Menu:", reply_markup=MAIN_MENU, parse_mode="HTML")

async def on_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (update.effective_user and update.effective_user.id in OWNER_IDS):
        return
    q = update.callback_query
    data = q.data or ""
    context.user_data.clear()

    if data == "m:status":
        await q.answer()
        await q.edit_message_text(status_text(), reply_markup=MAIN_MENU, parse_mode="HTML")
        return
    if data == "m:enable":
        store["enabled"] = True; save_store(); reschedule_job(context.application)
        await q.answer("Enabled")
        await q.edit_message_text("Auto-posting enabled ✅", reply_markup=MAIN_MENU)
        return
    if data == "m:disable":
        store["enabled"] = False; save_store(); reschedule_job(context.application)
        await q.answer("Disabled")
        await q.edit_message_text("Auto-posting disabled ⏹️", reply_markup=MAIN_MENU)
        return
    if data == "m:interval":
        await q.answer()
        await q.edit_message_text("⏰ Please send the interval (e.g. 15m, 2h, 90)\nMinimum is 60 seconds.", reply_markup=None)
        context.user_data["awaiting_interval"] = True
        return
    if data == "m:message":
        await q.answer()
        await q.edit_message_text("✍️ Please send the new message (Telegram formatting preserved).", reply_markup=None)
        context.user_data["awaiting_message"] = True
        return
    if data == "m:photo":
        await q.answer()
        await q.edit_message_text("🖼️ Send photo link, file_id or 'none'.", reply_markup=None)
        context.user_data["awaiting_photo"] = True
        return
    if data == "m:buttons":
        btns = "\n".join([f"▫️ {l} → {u}" for l, u in store["buttons"]]) or "None"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Button", callback_data="b:add")],
            [InlineKeyboardButton("🧹 Clear All Buttons", callback_data="b:clear")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="m:menu")]
        ] + [
            [InlineKeyboardButton(f"❌ Remove: {label}", callback_data=f"b:del:{i}")]
            for i, (label, url) in enumerate(store["buttons"])
        ])
        await q.answer()
        await q.edit_message_text(f"🔘 Current buttons:\n{btns}\n\nUse buttons below to manage.", reply_markup=kb)
        return
    if data == "m:groups":
        ids = store.get("groups", [])
        if not ids:
            await q.answer()
            await q.edit_message_text("No groups added yet.\nUse the button below to add.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Group", callback_data="g:add")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="m:menu")]
            ]))
            return
        kb = [
            [InlineKeyboardButton("➕ Add Group", callback_data="g:add")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="m:menu")]
        ] + [
            [InlineKeyboardButton(f"❌ Remove {gid}", callback_data=f"g:del:{gid}")]
            for gid in ids
        ]
        await q.answer()
        await q.edit_message_text(
            "👥 Added Groups:\n" + "\n".join([str(x) for x in ids]) + "\n\nUse buttons below to manage.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return
    if data == "m:help":
        await q.answer()
        await q.edit_message_text(
            """❓ Quick Guide:
• Add Group: Button or /add_group_link
• Set Message: Button or /set_message
• Set Interval: Button or /set_interval
• Set Photo: Button or /set_photo
• Manage Buttons: Button or /set_buttons
• Enable/Disable: Related buttons
• Remove Group: Remove button beside each group
• Remove Button: Remove button beside each button
""",
            reply_markup=MAIN_MENU
        )
        return
    if data == "m:menu":
        await q.answer()
        await q.edit_message_text("🌟 Bot Management Menu:", reply_markup=MAIN_MENU, parse_mode="HTML")
        return

    # BUTTONS management
    if data.startswith("b:add"):
        await q.answer()
        await q.edit_message_text("To add a button, send: Label|https://url", reply_markup=None)
        context.user_data["awaiting_button"] = True
        return
    if data.startswith("b:del:"):
        idx = int(data.split(":")[2])
        if 0 <= idx < len(store["buttons"]):
            store["buttons"].pop(idx); save_store()
        btns = "\n".join([f"▫️ {l} → {u}" for l, u in store["buttons"]]) or "None"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Button", callback_data="b:add")],
            [InlineKeyboardButton("🧹 Clear All Buttons", callback_data="b:clear")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="m:menu")]
        ] + [
            [InlineKeyboardButton(f"❌ Remove: {label}", callback_data=f"b:del:{i}")]
            for i, (label, url) in enumerate(store["buttons"])
        ])
        await q.answer("Button removed")
        await q.edit_message_text(f"🔘 Current buttons:\n{btns}\n\nUse buttons below to manage.", reply_markup=kb)
        return
    if data.startswith("b:clear"):
        store["buttons"] = []; save_store()
        await q.answer("All buttons cleared")
        await q.edit_message_text("All buttons cleared. You can add new ones.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Button", callback_data="b:add")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="m:menu")]
        ]))
        return

    # GROUPS management
    if data.startswith("g:add"):
        await q.answer()
        await q.edit_message_text("Please send the group link, @username or id to add.", reply_markup=None)
        context.user_data["awaiting_group"] = True
        return
    if data.startswith("g:del:"):
        gid = int(data.split(":")[2])
        if gid in store["groups"]:
            store["groups"].remove(gid); save_store()
        ids = store.get("groups", [])
        kb = [
            [InlineKeyboardButton("➕ Add Group", callback_data="g:add")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="m:menu")]
        ] + [
            [InlineKeyboardButton(f"❌ Remove {gid}", callback_data=f"g:del:{gid}")]
            for gid in ids
        ]
        await q.answer("Group removed")
        await q.edit_message_text(
            "👥 Added Groups:\n" + ("\n".join([str(x) for x in ids]) if ids else "None") + "\n\nUse buttons below to manage.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

# ---------------- Interactive text input ------------------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    msg = update.message
    # INTERVAL input
    if context.user_data.get("awaiting_interval"):
        interval = msg.text.strip()
        try:
            seconds = parse_interval(interval)
            if seconds < 60:
                await msg.reply_text("Minimum interval is 60 seconds.")
            else:
                store["seconds"] = seconds; save_store(); reschedule_job(context.application)
                await msg.reply_text(f"Interval saved: {seconds} seconds ⏱️", reply_markup=MAIN_MENU)
        except Exception as e:
            await msg.reply_text(f"Invalid format: {e}")
        context.user_data.clear()
        return
    # MESSAGE input (entities preserved)
    if context.user_data.get("awaiting_message"):
        raw = msg.text or ""
        text = raw
        ents = []
        if msg.entities:
            for e in msg.entities:
                if e.type == "bot_command": continue
                d = {"type": e.type, "offset": e.offset, "length": e.length}
                if e.url: d["url"] = e.url
                if e.language: d["language"] = e.language
                ents.append(d)
        store["message"] = text
        store["entities"] = ents
        save_store()
        await msg.reply_text("New message saved ✍️", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return
    # PHOTO input
    if context.user_data.get("awaiting_photo"):
        arg = msg.text.strip()
        store["photo"] = None if arg.lower() == "none" else arg
        save_store()
        await msg.reply_text("Photo saved 🖼️", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return
    # BUTTON input
    if context.user_data.get("awaiting_button"):
        try:
            label, url = [x.strip() for x in msg.text.split("|", 1)]
            if (not label) or (not (url.startswith("http://") or url.startswith("https://"))):
                raise Exception()
        except Exception:
            await msg.reply_text("Bad format. Example: Shop|https://t.me/YourBot")
            return
        if len(store["buttons"]) >= 8:
            await msg.reply_text("Maximum 8 buttons allowed.")
            return
        store["buttons"].append([label, url]); save_store()
        await msg.reply_text("Button added ➕", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return
    # GROUP input
    if context.user_data.get("awaiting_group"):
        inp = msg.text.strip()
        try:
            ref = _normalize_chat_ref(inp)
            gid = await _resolve_chat_id(context, ref)
        except Exception as e:
            await msg.reply_text(f"Error adding group: {e}")
            return
        if gid not in store["groups"]:
            store["groups"].append(gid); save_store()
        await msg.reply_text(f"Group added ✅\nID: {gid}", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return

# ---------------- Group tracking (auto) -------------------------------------
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat: return
    status = update.my_chat_member.new_chat_member.status
    if status in ("member", "administrator", "restricted"):
        if chat.id not in store["groups"]:
            store["groups"].append(chat.id); save_store()
            print(f"[INFO] Added group {chat.id}")

# ---------------- Main ------------------------------------------------------
if __name__ == "__main__":
    start_health_server()

    jq = JobQueue()
    app = Application.builder().token(TOKEN).job_queue(jq).build()
    jq.set_application(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(on_menu_cb, pattern=r".*"))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, on_text))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.GROUPS, lambda u, c: None))

    reschedule_job(app)
    app.job_queue.run_repeating(_keepalive, interval=240, first=10, name="KEEPALIVE")

    print("Bot is running…")
    app.run_polling()
