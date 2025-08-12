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
# ---------------------------------------------------------------------------

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
# ---------------------------------------------------------------------------

# ---------------- Credentials ----------------------------------------------
TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").strip().split(",") if x.strip().isdigit()}
if not TOKEN or not OWNER_IDS:
    raise SystemExit("BOT_TOKEN and OWNER_IDS env vars are required. Example OWNER_IDS='123,456'")
# ---------------------------------------------------------------------------

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
# ---------------------------------------------------------------------------

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
# ---------------------------------------------------------------------------

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
# ---------------------------------------------------------------------------

def reschedule_job(app: Application):
    for j in app.job_queue.get_jobs_by_name("GLOBAL_POSTER"):
        j.schedule_removal()
    if store["enabled"]:
        app.job_queue.run_repeating(send_to_all_groups, interval=store["seconds"], first=0, name="GLOBAL_POSTER")

# ---------------- Commands --------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_owner(update):
        return await update.message.reply_text("Hi! Only the bot owners can change settings.")
    await send_menu(update, context)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    await send_menu(update, context)

async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Status", callback_data="m:status"),
         InlineKeyboardButton("Enable ✅", callback_data="m:enable"),
         InlineKeyboardButton("Disable ⏹️", callback_data="m:disable")],
        [InlineKeyboardButton("Set Interval", callback_data="m:hint_interval"),
         InlineKeyboardButton("Set Message", callback_data="m:hint_message")],
        [InlineKeyboardButton("Set Photo", callback_data="m:hint_photo"),
         InlineKeyboardButton("Buttons", callback_data="m:hint_buttons")],
        [InlineKeyboardButton("List Groups", callback_data="m:list_groups")]
    ])
    await update.message.reply_text("Bot menu:", reply_markup=kb)

async def on_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (update.effective_user and update.effective_user.id in OWNER_IDS):
        return
    q = update.callback_query
    data = q.data or ""
    if data == "m:status":
        mins = store["seconds"] // 60
        btns = "\n".join([f"- {l} → {u}" for l, u in store["buttons"]]) or "-"
        await q.answer()
        await q.edit_message_text(
            f"Status: {'Enabled' if store['enabled'] else 'Disabled'}\n"
            f"Interval: {store['seconds']} sec (~{mins} min)\n"
            f"Photo: {store['photo'] or '-'}\n"
            f"Message:\n{store['message']}\n\nButtons:\n{btns}"
        )
        return
    if data == "m:enable":
        store["enabled"] = True; save_store(); reschedule_job(context.application)
        await q.answer("Enabled")
        await q.edit_message_text("Auto-posting enabled ✅")
        return
    if data == "m:disable":
        store["enabled"] = False; save_store(); reschedule_job(context.application)
        await q.answer("Disabled")
        await q.edit_message_text("Auto-posting disabled ⏹️")
        return
    if data == "m:list_groups":
        ids = store.get("groups", [])
        text = "No groups stored yet." if not ids else "Groups:\n" + "\n".join(str(x) for x in ids)
        await q.answer()
        await q.edit_message_text(text)
        return
    if data == "m:hint_interval":
        await q.answer()
        await q.edit_message_text("Use:\n/set_interval 15m\n/set_interval 2h\n/set_interval 3600")
        return
    if data == "m:hint_message":
        await q.answer()
        await q.edit_message_text("Use:\n/set_message <your text>\nFormatting is preserved from Telegram entities.")
        return
    if data == "m:hint_photo":
        await q.answer()
        await q.edit_message_text("Use:\n/set_photo <url|file_id|none>")
        return
    if data == "m:hint_buttons":
        await q.answer()
        await q.edit_message_text("Use:\n/set_buttons Label1|https://a;Label2|https://b\n/add_button Label|https://url\n/clear_buttons")
        return

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
    store["enabled"] = True; save_store(); reschedule_job(context.application)
    await update.message.reply_text("Enabled ✅")

async def cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    store["enabled"] = False; save_store(); reschedule_job(context.application)
    await update.message.reply_text("Disabled ⏹️")

# ---------- entities-preserving set_message ----------
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
    await msg.reply_text("Message updated ✍️ (formatting preserved)")

async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if not context.args:
        return await update.message.reply_text("Usage: /set_interval 15m")
    try:
        seconds = parse_interval(context.args[0])
        if seconds < 60:
            return await update.message.reply_text("Minimum interval is 60 seconds.")
    except ValueError as e:
        return await update.message.reply_text(str(e))
    store["seconds"] = seconds; save_store(); reschedule_job(context.application)
    await update.message.reply_text("Interval updated ⏱️")

async def cmd_set_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if not context.args:
        return await update.message.reply_text("Usage: /set_photo <url|file_id|none>")
    arg = context.args[0].strip()
    store["photo"] = None if arg.lower() == "none" else arg
    save_store()
    await update.message.reply_text("Photo updated 🖼️")

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
    store["buttons"] = [list(p) for p in pairs][:8]; save_store()
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

# ---------------- Group tracking -------------------------------------------
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat: return
    status = update.my_chat_member.new_chat_member.status
    if status in ("member", "administrator", "restricted"):
        if chat.id not in store["groups"]:
            store["groups"].append(chat.id); save_store()
            print(f"[INFO] Added group {chat.id}")

async def cmd_list_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    ids = store.get("groups", [])
    text = "No groups stored yet." if not ids else "Groups:\n" + "\n".join(str(x) for x in ids)
    await update.message.reply_text(text)

async def cmd_add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if not context.args:
        return await update.message.reply_text("Usage: /add_group <id>")
    try:
        gid = int(context.args[0])
    except:
        return await update.message.reply_text("Invalid group id.")
    if gid not in store["groups"]:
        store["groups"].append(gid); save_store()
    await update.message.reply_text(f"Group {gid} added ✅")

async def cmd_add_group_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if not context.args:
        return await update.message.reply_text("Usage: /add_group_link <link|@username|id>")
    inp = " ".join(context.args).strip()
    try:
        ref = _normalize_chat_ref(inp)
        gid = await _resolve_chat_id(context, ref)
    except ValueError as ve:
        return await update.message.reply_text(f"Invalid link/username/id: {ve}")
    except Exception as e:
        return await update.message.reply_text(f"Could not resolve: {e}")
    if gid not in store["groups"]:
        store["groups"].append(gid); save_store()
    await update.message.reply_text(f"Group added ✅\nID: {gid}")

# ---------------- Main ------------------------------------------------------
if __name__ == "__main__":
    start_health_server()

    jq = JobQueue()
    app = Application.builder().token(TOKEN).job_queue(jq).build()
    jq.set_application(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(on_menu_cb, pattern=r"^m:"))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("enable", cmd_enable))
    app.add_handler(CommandHandler("disable", cmd_disable))
    app.add_handler(CommandHandler("set_message", cmd_set_message))
    app.add_handler(CommandHandler("set_interval", cmd_set_interval))
    app.add_handler(CommandHandler("set_photo", cmd_set_photo))
    app.add_handler(CommandHandler("set_buttons", cmd_set_buttons))
    app.add_handler(CommandHandler("add_button", cmd_add_button))
    app.add_handler(CommandHandler("clear_buttons", cmd_clear_buttons))
    app.add_handler(CommandHandler("list_groups", cmd_list_groups))
    app.add_handler(CommandHandler("add_group", cmd_add_group))
    app.add_handler(CommandHandler("add_group_link", cmd_add_group_link))

    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.GROUPS, lambda u, c: None))

    reschedule_job(app)
    app.job_queue.run_repeating(_keepalive, interval=240, first=10, name="KEEPALIVE")

    print("Bot is running…")
    app.run_polling()
