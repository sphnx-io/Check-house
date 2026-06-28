# BSC Housing Checker

Monitors the BSC member portal for available spots in preferred co-ops and sends an email (+ optional Telegram) when one opens up.

---

## Setup

### 1. Install dependencies (inside WSL)

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | What to put |
|---|---|
| `BSC_LOGIN_URL` | `https://bsc.rms-inc.com/page/login` |
| `BSC_USERNAME` | Your BSC username or email |
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | A Google "App Password" (16 chars, no spaces). Generate one at: myaccount.google.com → Security → 2-Step Verification → App passwords |
| `NOTIFY_EMAIL` | Where to send alerts (can be same as Gmail) |
| `PREFERRED_HOUSES` | Comma-separated, exact names as they appear on the BSC site |

### 3. First run — debug mode (find the login selectors)

```bash
python checker.py --debug
```

The browser will open visibly. At each pause, use DevTools (F12) to find the correct CSS selectors for:
- The username input field
- The "Forgot Password" link
- The password input field  
- The submit/login button
- The password-change fields (if a prompt appears after first login)

Then edit the `# SELECTORS` section at the top of `checker.py` with what you find.

### 4. Run headless once to validate

```bash
python checker.py
```

Check `checker.log` to see if it completed successfully.

---

## Scheduling with cron (WSL)

Open a WSL terminal and run:

```bash
crontab -e
```

Add this line to run every 25 minutes:

```
*/25 * * * * cd /mnt/c/Users/Hippo/Desktop/Project_PP/Check-house && /usr/bin/python3 checker.py >> /dev/null 2>&1
```

> **Important — WSL cron note**: WSL does not start the cron daemon automatically. Add this to your `~/.bashrc` or `~/.profile` so it starts when WSL opens:
> ```bash
> sudo service cron start
> ```
> Or use Windows Task Scheduler to launch a WSL cron-start command at boot.

---

## Files

| File | Purpose |
|---|---|
| `checker.py` | Main script |
| `.env` | Your credentials (never commit this) |
| `last_state.json` | Auto-generated — saved state from last run |
| `checker.log` | Auto-generated — full execution log |
| `debug_page_dump.html` | Auto-generated on error — raw page source for debugging |

---

## How it works

1. Opens the BSC login page, enters your username, clicks "Forgot Password"
2. Polls your Gmail inbox (IMAP) for the temp-password email
3. Logs in with the temp password, changes it to a new random one
4. Clicks "Select My Co-op for Fall"
5. Extracts the availability JSON embedded in the page (`var TreeDataJSON = [...]`)
6. Compares with `last_state.json`
7. Sends a notification if a preferred house has new available spots
