# Telegram Auto Poster for Render

## How to deploy
1. Create a new Web Service on Render and connect this repo
2. Set Environment to Python
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python bot.py`
5. Add env vars
   - `BOT_TOKEN` e.g. `123456:ABC...`
   - `OWNER_IDS` e.g. `7169307026,1604088446`
6. Choose Free plan and Deploy

## Owner commands
- /enable
- /disable
- /set_message <text>
- /set_interval <15m|2h|1d|secs>
- /set_photo <url|file_id|none>
- /set_buttons Label|https://a;Label2|https://b
- /add_button Label|https://...
- /clear_buttons
- /status
