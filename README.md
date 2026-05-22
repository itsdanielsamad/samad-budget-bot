# Vault_42_bot — Samad Family Budget Telegram bot

A Telegram bot that listens for spending messages, parses them with Claude, logs to your Google Sheet, and replies with the remaining balance.

## Architecture

```
[Telegram message: "Carrefour 250"]
      ↓
@Vault_42_bot (this app, polling Telegram)
      ↓ calls Gemini API to parse + categorize
[Returns: {amount: 250, vendor: "Carrefour", line_item: "Groceries", category: "Food, Health & Lifestyle", confidence: "High"}]
      ↓
Appends row to "Spent Bucket" tab
      ↓
Reads "Actual Monthly" from Expenses tab (SUMIFS auto-updated)
      ↓
[Telegram reply: "Logged AED 250 to Groceries. Remaining: AED 1,750."]
```

## Files

| File | What it does |
|---|---|
| `main.py` | Telegram entry point, handlers, orchestrator |
| `config.py` | Loads env vars |
| `sheets_client.py` | Google Sheets API — read Vendor Memory + Expenses, append to Spent Bucket |
| `llm_parser.py` | Calls Gemini API to parse messages into structured transactions (free tier) |
| `requirements.txt` | Python deps |
| `.env.example` | Template — copy to `.env` locally |
| `railway.json` / `Procfile` | Deploy configs |

## One-time setup

### 1. Gemini API key (free, no credit card)

1. Go to https://aistudio.google.com/apikey
2. Sign in with any Google account → **Create API key** → copy the `AIzaSy...` value.
3. Save as env var `GEMINI_API_KEY`.

Free tier (as of 2026) gives ~1,500 requests/day on Gemini 2.5 Flash — easily enough for household use.

### 2. Google service account (gives the bot write access to your sheet)

1. Go to https://console.cloud.google.com — create a new project (e.g. "samad-budget-bot").
2. Enable APIs: **Google Sheets API** and **Google Drive API**.
3. Go to **IAM & Admin → Service Accounts → Create**. Name it e.g. `vault42-bot`.
4. Once created, open the service account → **Keys → Add Key → JSON**. Download the `.json` file.
5. Open the file — find the `"client_email"` field, looks like `vault42-bot@samad-budget-bot.iam.gserviceaccount.com`.
6. Open your Google Sheet → **Share** → paste that email → give **Editor** access.
7. Copy the **entire JSON content** of the key file. That whole JSON string is the value of env var `GOOGLE_SERVICE_ACCOUNT_JSON`.

### 3. Telegram bot token

You already have @Vault_42_bot. The token comes from @BotFather:
1. Open Telegram → @BotFather → `/mybots` → pick `@Vault_42_bot` → `API Token`.
2. Save as env var `TELEGRAM_BOT_TOKEN`.

### 4. Your Telegram user IDs

The bot only accepts messages from allowlisted users (you + Danos).
1. Open Telegram → @userinfobot → it tells you your numeric user ID.
2. Do the same for Danos.
3. Set env var `ALLOWED_TELEGRAM_USER_IDS=11111111,22222222`.
4. Set env var `PAYER_MAP=11111111:Nasos,22222222:Danos` so transactions are tagged correctly.

### 5. Spreadsheet ID

Already known: `1wRKnfjSBBukbKaXWv_rQ5T44faAZz1Dej7ZVE1KeFdE`.

## Deploy to Railway (free tier)

1. Go to https://railway.app → log in with GitHub.
2. **New Project → Deploy from GitHub repo** — first push this folder to a private GitHub repo:
   ```bash
   cd vault_42_bot
   git init
   git add .
   git commit -m "Initial commit"
   gh repo create samad-vault42-bot --private --source=. --push
   ```
3. In Railway: link the repo. Railway auto-detects Python.
4. **Variables** tab → add each env var from `.env.example`:
   - `TELEGRAM_BOT_TOKEN`
   - `GEMINI_API_KEY`
   - `GEMINI_MODEL` = `gemini-2.5-flash`
   - `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the whole JSON — Railway supports multi-line)
   - `SPREADSHEET_ID` = `1wRKnfjSBBukbKaXWv_rQ5T44faAZz1Dej7ZVE1KeFdE`
   - `ALLOWED_TELEGRAM_USER_IDS`, `PAYER_MAP`
5. **Deploy** — Railway runs `python main.py`. Tail the logs; you should see `Bot ready. Polling…`
6. Send a Telegram message to @Vault_42_bot — should reply.

## Run locally (test before deploy)

```bash
cd vault_42_bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your real values
python main.py
```

## First-time smoke test

1. `/start` — bot should greet you.
2. `/balance Groceries` — should print budget / spent MTD / remaining.
3. Send `Carrefour 30` — bot should auto-log (High confidence via Vendor Memory) and reply with remaining.
4. Send `Random place 50` — bot should ask for confirmation (Low/Medium confidence).
5. Send `/undo` — last logged transaction is marked Reversed in the sheet.

## Adding/correcting categorizations

If the bot makes a wrong call, the simplest fix: edit the row directly in the Spent Bucket tab. The bot reads the sheet, so any manual edit is respected next time it parses similar text.

To teach it a new vendor permanently: add a row to the **Vendor Memory** tab. The bot refreshes its cache hourly (and on restart).

## Commands

| Command | What it does |
|---|---|
| `/start` | Greeting + quick guide |
| `/help` | Usage examples |
| `/balance <line item>` | Show budget, spent MTD, remaining |
| `/undo` | Mark last-logged transaction in this session as Reversed |

## Roadmap (future sessions)

- [ ] Carry Over auto-drain on the 1st of each month
- [ ] Weekly digest scheduled for Sunday 20:00 UAE
- [ ] Budget alert when category hits 80% / 100% / 120%
- [ ] `/report` command — month-end summary
- [ ] Multi-currency parsing (USD/EUR auto-convert to AED)
