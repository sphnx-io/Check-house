# BSC Housing Checker

Monitors the BSC member portal for available spots in preferred co-ops and sends a **Telegram + email** notification the moment a spot opens up.

Runs as a continuous watcher (keeps the browser session alive between checks — no re-login every time).

---

## Setup

### 1. Install dependencies (Windows, in the project virtualenv)

```powershell
pip install -r requirements.txt
playwright install chromium
```

### 2. Gmail API (one-time)

The script reads your email via the Gmail API to retrieve the BSC temporary password when needed.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → New project → `BSC Checker`
2. APIs & Services → Library → enable **Gmail API**
3. APIs & Services → Credentials → Create OAuth client ID → **Desktop app**
4. Download JSON → rename to `gmail_credentials.json` → place in this folder
5. Add your email as a test user: OAuth consent screen → Test users

```powershell
python checker.py --setup
```

A browser opens — log in with your **account** and grant Gmail permissions. Token saved to `gmail_token.json`.

### 3. Configure `.env`

```powershell
copy .env.example .env
```

Edit `.env`:

| Variable | Value |
|---|---|
| `BSC_LOGIN_URL` | `https://bsc.rms-inc.com/page/login` |
| `BSC_USERNAME` | Your email (used to log in to BSC) |
| `BSC_PASSWORD` | Leave blank first time; auto-filled after first login |
| `GMAIL_ADDRESS` | Your Berkeley email (for Gmail API) |
| `NOTIFY_EMAIL` | Where to send email alerts |
| `PREFERRED_HOUSES` | Comma-separated house names (see note below) |
| `CHECK_INTERVAL_MINUTES` | Minutes between checks (default: 25) |
| `TELEGRAM_BOT_TOKEN` | From @BotFather on Telegram (optional) |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID (optional) |

**House names** must match what appears in the BSC portal tree. The script uses fuzzy matching (case-insensitive, handles hyphens), so `Hillegass Parker` will match `Hillegass-Parker`.

### 4. Telegram (optional but recommended)

1. Message `@BotFather` on Telegram → `/newbot` → get your token
2. Send any message to your bot, then open:
   `https://api.telegram.org/botTOKEN/getUpdates`
3. Copy the `chat.id` value
4. Add both to `.env`

### 5. First run (debug mode — visible browser)

```powershell
python checker.py --debug
```

Verify the login and navigation work. The browser is visible so you can inspect any issues.

### 6. Run as continuous watcher

```powershell
python checker.py --loop
```

Or with a custom interval:

```powershell
python checker.py --loop --interval 10
```

Stop with **Ctrl+C**.

---

## How it works

1. **Login** — tries `BSC_PASSWORD` directly; falls back to "Forgot Password" flow (fetches temp password from email via Gmail API, changes it to a new random password, saves it to `.env`)
2. **Navigate** — clicks "Select My Co-op for Fall" → navigates through 4 wizard steps
3. **Scrape** — extracts `var TreeDataJSON = [...]` embedded in the page (only buildings with available spots appear)
4. **Compare** — diffs against `last_state.json`
5. **Notify** — sends Telegram + email if a preferred house goes from 0 → available
6. **Sleep** — waits `CHECK_INTERVAL_MINUTES` then repeats (session stays alive)

---

## Files

| File | Purpose |
|---|---|
| `checker.py` | Main script |
| `.env` | Your credentials — **never commit** |
| `gmail_credentials.json` | Google OAuth app credentials — **never commit** |
| `gmail_token.json` | Auto-generated OAuth token — **never commit** |
| `last_state.json` | Auto-generated — availability snapshot from last run |
| `checker.log` | Auto-generated — full execution log with timestamps |

---

## CLI reference

```
python checker.py              # single run (headless)
python checker.py --loop       # continuous watcher (recommended)
python checker.py --loop --interval 10   # check every 10 min
python checker.py --debug      # single run with visible browser + pauses
python checker.py --setup      # one-time Gmail OAuth2 authorization
```
