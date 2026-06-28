#!/usr/bin/env python3
"""
BSC Housing availability monitor.

Flow each run:
  1. Open login page, enter username, trigger "Forgot Password"
  2. Poll Berkeley Gmail (via Gmail API OAuth2) for the temp-password email
  3. Log in with temp password
  4. Handle the forced password-change prompt
  5. Navigate to "Select My Co-op for Fall"
  6. Extract TreeDataJSON from page source
  7. Compare with last known state; notify if a preferred house has new spots

First run: python checker.py --setup   → one-time Gmail OAuth2 authorization
Normal run: python checker.py
Debug run:  python checker.py --debug  → visible browser + interactive pauses
"""

import argparse
import base64
import json
import logging
import os
import re
import secrets
import string
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

BSC_LOGIN_URL    = os.environ["BSC_LOGIN_URL"]
BSC_USERNAME     = os.environ["BSC_USERNAME"]
BSC_PASSWORD     = os.environ.get("BSC_PASSWORD", "").strip()   # optional known password
GMAIL_ADDRESS    = os.environ["GMAIL_ADDRESS"]       # Berkeley email
NOTIFY_EMAIL     = os.environ["NOTIFY_EMAIL"]
PREFERRED_HOUSES = [h.strip() for h in os.environ.get("PREFERRED_HOUSES", "").split(",") if h.strip()]
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT    = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE   = Path(__file__).parent / "last_state.json"
LOG_FILE     = Path(__file__).parent / "checker.log"
CREDS_FILE   = Path(__file__).parent / "gmail_credentials.json"
TOKEN_FILE   = Path(__file__).parent / "gmail_token.json"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# ─────────────────────────────────────────────────────────────────────────────
#  SELECTORS — fill these in after running:  python checker.py --debug
# ─────────────────────────────────────────────────────────────────────────────

SEL_USERNAME_FIELD   = '[rmsmodel="UserNameEntry"]'
SEL_FORGOT_PWD_LINK  = "#ForgotPasswordButton"
SEL_FORGOT_EMAIL_FLD = None
SEL_FORGOT_SUBMIT    = None   # ForgotPasswordButton is the submit — no second click needed
SEL_PASSWORD_FIELD   = '[rmsmodel="PasswordEntry"]'
SEL_LOGIN_BTN        = "#LoginButton"
SEL_NEW_PWD_FIELD    = "#txtResetPassword"
SEL_CONFIRM_PWD_FLD  = "#txtRetypeResetPassword"
SEL_CHANGE_PWD_BTN   = "#btnResetPassword"
SEL_HOUSING_LINK     = "text=Select My Co-op for Fall"

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(debug: bool):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

log = logging.getLogger(__name__)

# ── Gmail API ─────────────────────────────────────────────────────────────────

def get_gmail_service():
    """Return an authenticated Gmail API service, refreshing/creating the token as needed."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                raise FileNotFoundError(
                    f"{CREDS_FILE} not found. "
                    "Download it from Google Cloud Console and place it in the project folder. "
                    "Then run: python checker.py --setup"
                )
            flow  = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


TEMP_PWD_SUBJECT = "BSC Member Portal Password Reset"
TEMP_PWD_RE      = re.compile(r"Temporary Password:\s*(\S+)", re.IGNORECASE)


def _decode_gmail_body(msg: dict) -> str:
    """
    Extract text from a Gmail API message.
    Priority: text/plain > text/html (stripped) > snippet.
    """
    def walk(part, prefer_plain=True):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data", "")
        if mime == "text/plain" and data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        if mime == "text/html" and data and not prefer_plain:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            # Strip HTML tags with a simple regex — good enough for a password email
            return re.sub(r"<[^>]+>", " ", html)
        for sub in part.get("parts", []):
            result = walk(sub, prefer_plain)
            if result:
                return result
        return ""

    payload = msg.get("payload", {})

    # Try plain text first
    text = walk(payload, prefer_plain=True)
    if text.strip():
        return text

    # Fall back to HTML (stripped)
    text = walk(payload, prefer_plain=False)
    if text.strip():
        return text

    # Last resort: Gmail's own snippet preview
    snippet = msg.get("snippet", "")
    log.debug("Body empty, using snippet: %s", snippet[:200])
    return snippet


def fetch_temp_password(service, since_ts: int, max_wait: int = 120, poll_interval: int = 10) -> str:
    """
    Poll Berkeley Gmail for the BSC temp-password email.
    Uses internalDate (Gmail delivery timestamp) to filter — more reliable than the
    `after:` query operator which uses the email's own Date header (can differ from our clock).
    since_ts: Unix timestamp in seconds — only accept emails delivered at or after this time.
    """
    deadline     = time.time() + max_wait
    since_ts_ms  = (since_ts - 30) * 1000   # 30s buffer for clock skew, in milliseconds

    log.info("Waiting for temp-password email (internalDate >= %d ms, up to %ds)…",
             since_ts_ms, max_wait)

    try:
        profile = service.users().getProfile(userId="me").execute()
        log.info("Gmail API connected as: %s", profile.get("emailAddress"))
    except Exception as exc:
        log.warning("Could not get Gmail profile: %s", exc)

    while time.time() < deadline:
        try:
            # Broad search — we filter by internalDate ourselves below
            result = service.users().messages().list(
                userId="me",
                q=f'subject:"{TEMP_PWD_SUBJECT}" newer_than:60m',
            ).execute()
            found = result.get("messages", [])
            log.debug("Gmail search returned %d candidate(s).", len(found))

            for item in found:
                # Fetch metadata first (cheap) to check delivery timestamp
                meta = service.users().messages().get(
                    userId="me", id=item["id"], format="metadata",
                ).execute()
                internal_date_ms = int(meta.get("internalDate", 0))
                if internal_date_ms < since_ts_ms:
                    log.debug("Skipping email delivered before ForgotPassword click (%d < %d).",
                              internal_date_ms, since_ts_ms)
                    continue

                # Good timestamp — fetch full content
                msg  = service.users().messages().get(
                    userId="me", id=item["id"], format="full"
                ).execute()
                body = _decode_gmail_body(msg)
                log.debug("Body preview: %s", repr(body[:200]))
                m    = TEMP_PWD_RE.search(body)
                if m:
                    log.info("Temp password found (email internalDate=%d).", internal_date_ms)
                    return m.group(1)
                log.debug("Pattern not matched in body.")

        except Exception as exc:
            log.warning("Gmail API error: %s", exc)

        remaining = int(deadline - time.time())
        log.debug("Not found yet, retrying in %ds (%ds left)…", poll_interval, remaining)
        time.sleep(poll_interval)

    raise RuntimeError(f"Timed out after {max_wait}s waiting for BSC temp-password email.")

# ── Send notification email via Gmail API ─────────────────────────────────────

def send_email_via_api(service, subject: str, body: str):
    msg            = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = NOTIFY_EMAIL
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info("Email notification sent to %s.", NOTIFY_EMAIL)

# ── Password helpers ──────────────────────────────────────────────────────────

def generate_password(length: int = 16) -> str:
    # Avoid # and % — both break .env parsing
    special = "!@$^&*"
    alphabet = string.ascii_letters + string.digits + special
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.isupper() for c in pwd) and any(c.islower() for c in pwd)
                and any(c.isdigit() for c in pwd) and any(c in special for c in pwd)):
            return pwd

# ── Playwright: login flow ────────────────────────────────────────────────────

def _click(page, selector: str, timeout: int = 10_000):
    log.debug("Clicking: %s", selector)
    page.wait_for_selector(selector, timeout=timeout).click()


def _fill(page, selector: str, value: str, timeout: int = 10_000):
    log.debug("Filling: %s", selector)
    page.wait_for_selector(selector, timeout=timeout).fill(value)


def login_and_navigate(playwright, service, debug: bool):
    browser = playwright.chromium.launch(headless=not debug)
    context = browser.new_context()
    page    = context.new_page()
    page.set_default_timeout(15_000)

    # ── 1. Open login page ────────────────────────────────────────────────────
    log.info("Opening login page: %s", BSC_LOGIN_URL)
    page.goto(BSC_LOGIN_URL, wait_until="networkidle")
    if debug:
        input("[DEBUG] Login page loaded. Press Enter…")

    # ── 1b. Try direct login with known password (skips forgot-password flow) ─
    if BSC_PASSWORD:
        log.info("BSC_PASSWORD found — trying direct login first…")
        page.locator(SEL_USERNAME_FIELD).click()
        page.locator(SEL_USERNAME_FIELD).press_sequentially(BSC_USERNAME, delay=40)
        page.locator(SEL_PASSWORD_FIELD).click()
        page.locator(SEL_PASSWORD_FIELD).press_sequentially(BSC_PASSWORD, delay=40)
        page.locator(SEL_LOGIN_BTN).click()
        page.wait_for_load_state("networkidle")
        log.info("Direct login attempt. URL: %s", page.url)

        # Detect success: on login failure, #ForgotPasswordButton stays visible
        # On password-change page it's hidden; on dashboard it doesn't exist — both = success
        page.wait_for_timeout(2000)
        login_form_gone = not page.locator(SEL_FORGOT_PWD_LINK).is_visible()
        log.info("Direct login detection: ForgotPasswordButton visible=%s → success=%s",
                 not login_form_gone, login_form_gone)

        if login_form_gone:
            log.info("Direct login succeeded — proceeding to password-change step.")
            return _post_login(page, service, debug)

        log.warning("Direct login failed — falling back to forgot-password flow.")
        page.goto(BSC_LOGIN_URL, wait_until="networkidle")

    # ── 2. Trigger forgot-password ────────────────────────────────────────────
    log.info("Entering username and clicking Forgot Password…")
    _fill(page, SEL_USERNAME_FIELD, BSC_USERNAME)
    forgot_clicked_at = int(time.time())   # record timestamp BEFORE the click
    _click(page, SEL_FORGOT_PWD_LINK)
    page.wait_for_load_state("networkidle")
    if SEL_FORGOT_EMAIL_FLD:
        _fill(page, SEL_FORGOT_EMAIL_FLD, GMAIL_ADDRESS)
    if SEL_FORGOT_SUBMIT:
        _click(page, SEL_FORGOT_SUBMIT)
        page.wait_for_load_state("networkidle")
    log.info("Forgot-password submitted. URL: %s", page.url)
    if debug:
        input("[DEBUG] Forgot-password done. Check a confirmation appeared. Press Enter…")

    # ── 3. Poll Gmail for temp password ───────────────────────────────────────
    temp_pwd = fetch_temp_password(service, since_ts=forgot_clicked_at)

    # ── 4. Log in with temp password ──────────────────────────────────────────
    # Reload the login page for a fresh session token
    log.info("Reloading login page for fresh session…")
    page.goto(BSC_LOGIN_URL, wait_until="networkidle")
    log.info("Logging in with temp password…")

    # Use press_sequentially (Locator API) to simulate real keystrokes
    page.locator(SEL_USERNAME_FIELD).click()
    page.locator(SEL_USERNAME_FIELD).press_sequentially(BSC_USERNAME, delay=40)

    page.locator(SEL_PASSWORD_FIELD).click()
    page.locator(SEL_PASSWORD_FIELD).press_sequentially(temp_pwd, delay=40)

    page.locator(SEL_LOGIN_BTN).click()
    page.wait_for_load_state("networkidle")
    log.info("Post-login URL: %s", page.url)

    page.wait_for_timeout(2000)
    still_on_login = page.locator(SEL_FORGOT_PWD_LINK).is_visible()
    if still_on_login:
        log.warning("Login FAILED (login form still visible). Trying Enter key instead…")
        page.goto(BSC_LOGIN_URL, wait_until="networkidle")
        page.locator(SEL_USERNAME_FIELD).click()
        page.locator(SEL_USERNAME_FIELD).press_sequentially(BSC_USERNAME, delay=40)
        page.locator(SEL_PASSWORD_FIELD).click()
        page.locator(SEL_PASSWORD_FIELD).press_sequentially(temp_pwd, delay=40)
        page.locator(SEL_PASSWORD_FIELD).press("Enter")
        page.wait_for_load_state("networkidle")
        log.info("Post-Enter-key URL: %s", page.url)

    if debug:
        input("[DEBUG] Logged in. Check if a password-change prompt appeared. Press Enter…")

    return _post_login(page, service, debug)


def _handle_password_change(page, service):
    """Handle forced password-change prompt if present, then return (on dashboard)."""
    if SEL_NEW_PWD_FIELD and page.locator(SEL_CHANGE_PWD_BTN).is_visible():
        new_pwd = generate_password()
        log.info("Password change required. Changing (first 4: %s…)", new_pwd[:4])
        page.locator(SEL_NEW_PWD_FIELD).click()
        page.locator(SEL_NEW_PWD_FIELD).press_sequentially(new_pwd, delay=40)
        if SEL_CONFIRM_PWD_FLD:
            page.locator(SEL_CONFIRM_PWD_FLD).click()
            page.locator(SEL_CONFIRM_PWD_FLD).press_sequentially(new_pwd, delay=40)
        if SEL_CHANGE_PWD_BTN:
            page.locator(SEL_CHANGE_PWD_BTN).click()
            page.wait_for_load_state("networkidle")
        _save_password_to_env(new_pwd)
        log.info("Password changed and saved to .env.")
    else:
        log.info("No password change required — already on dashboard.")


def navigate_to_availability(page):
    """From the BSC dashboard, navigate to the housing availability page (TreeDataJSON)."""
    log.info("Clicking housing link…")
    _click(page, SEL_HOUSING_LINK, timeout=20_000)
    page.wait_for_load_state("networkidle")
    log.info("On housing landing page. URL: %s", page.url)

    for step in range(1, 9):
        try:
            page.wait_for_function(
                "document.body.innerHTML.includes('TreeDataJSON')",
                timeout=5_000,
            )
            log.info("TreeDataJSON found after %d click(s).", step - 1)
            return
        except PlaywrightTimeout:
            pass

        has_next   = page.locator("#NavButtonNext").count() > 0
        has_finish = page.locator("#NavButtonFinish").count() > 0

        if not has_next and not has_finish:
            log.info("No nav button on step %d — waiting for TreeDataJSON…", step)
            try:
                page.wait_for_function(
                    "document.body.innerHTML.includes('TreeDataJSON')", timeout=30_000)
                log.info("TreeDataJSON loaded.")
            except PlaywrightTimeout:
                log.warning("TreeDataJSON not found after 30s — trying extraction anyway.")
            return

        btn = "#NavButtonFinish" if (has_finish and not has_next) else "#NavButtonNext"
        log.info("Clicking %s (step %d)…", btn, step)
        _click(page, btn, timeout=15_000)
        page.wait_for_timeout(200)
        page.evaluate("var c=document.querySelector('#LiveStepContainer'); if(c) c.innerHTML=''")
        try:
            page.wait_for_function(
                "document.querySelector('#LiveStepContainer') && "
                "document.querySelector('#LiveStepContainer').children.length > 0",
                timeout=30_000,
            )
        except PlaywrightTimeout:
            log.warning("LiveStepContainer still empty after %s click.", btn)
        page.wait_for_timeout(2000)
        log.info("URL after step %d: %s", step, page.url)
    log.warning("Reached max steps (8) without finding TreeDataJSON.")


def _post_login(page, service, debug: bool):
    """After a successful login: handle password change, then navigate to availability."""
    _handle_password_change(page, service)
    if debug:
        input("[DEBUG] Post-login. Verify portal dashboard is visible. Press Enter…")
    navigate_to_availability(page)
    if debug:
        input("[DEBUG] Room selection page loaded. Press Enter to extract…")
    return page


def _save_password_to_env(new_pwd: str):
    """Update BSC_PASSWORD in the .env file with the newly generated password."""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    text = env_file.read_text(encoding="utf-8")
    if re.search(r"^BSC_PASSWORD\s*=", text, re.MULTILINE):
        text = re.sub(r"^(BSC_PASSWORD\s*=).*$", rf'\g<1>"{new_pwd}"', text, flags=re.MULTILINE)
    else:
        text += f'\nBSC_PASSWORD="{new_pwd}"\n'
    env_file.write_text(text, encoding="utf-8")
    log.info("BSC_PASSWORD updated in .env.")

# ── Scraping ──────────────────────────────────────────────────────────────────

TREE_JSON_RE = re.compile(r"var\s+TreeDataJSON\s*=\s*(\[.+?\]);", re.DOTALL)


def _walk_tree(nodes: list, result: dict):
    for node in nodes:
        if node.get("LivingAreaType") == 3:
            result[node["LivingAreaName"]] = node.get("AvailableCount", 0)
        if node.get("ChildList"):
            _walk_tree(node["ChildList"], result)


def extract_availability(page) -> dict:
    content = page.content()
    m = TREE_JSON_RE.search(content)
    if not m:
        dump = Path(__file__).parent / "debug_page_dump.html"
        dump.write_text(content, encoding="utf-8")
        raise RuntimeError(f"TreeDataJSON not found. Raw page saved to {dump}.")
    availability: dict = {}
    _walk_tree(json.loads(m.group(1)), availability)
    log.info("Scraped %d buildings with available spots.", len(availability))
    return availability

# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

# ── Change detection ──────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    """Normalize house name for fuzzy matching (lowercase, collapse separators)."""
    return re.sub(r"[\s\-_]+", " ", name.lower()).strip()


def _find_in_state(house: str, state: dict) -> tuple:
    """Exact then fuzzy lookup. Returns (matched_key, count)."""
    if house in state:
        return house, state[house]
    hn = _norm(house)
    for key, count in state.items():
        kn = _norm(key)
        if kn == hn or hn in kn or kn in hn:
            return key, count
    return house, 0


def detect_changes(old: dict, new: dict) -> list:
    """
    Change events for PREFERRED_HOUSES.
    Fuzzy name matching handles hyphen/space/case variations.
    """
    changes = []
    for house in PREFERRED_HOUSES:
        _, old_count        = _find_in_state(house, old)
        matched_key, new_count = _find_in_state(house, new)
        if old_count != new_count:
            changes.append({
                "house":            house,
                "matched_as":       matched_key,
                "old_count":        old_count,
                "new_count":        new_count,
                "became_available": new_count > 0 and old_count == 0,
                "freed_more":       new_count > old_count > 0,
            })
    return changes

# ── Notifications ─────────────────────────────────────────────────────────────

def _format_message(changes: list) -> tuple:
    lines, newly_available = [], []
    for c in changes:
        if c["became_available"]:
            lines.append(f"✅ {c['house']} — {c['new_count']} spot(s) now available!")
            newly_available.append(c["house"])
        elif c["freed_more"]:
            lines.append(f"📈 {c['house']} — {c['old_count']} → {c['new_count']} spots")
        else:
            lines.append(f"📉 {c['house']} — {c['old_count']} → {c['new_count']} spots")
    ts      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body    = "\n".join(lines) + f"\n\nChecked at {ts}\n{BSC_LOGIN_URL}"
    subject = ("🏠 BSC SPOT AVAILABLE — " + ", ".join(newly_available)
               if newly_available else
               "BSC Housing update — " + ", ".join(c["house"] for c in changes))
    return subject, body


def send_telegram(text: str):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT, "text": text},
        timeout=10,
    )
    if resp.ok:
        log.info("Telegram notification sent.")
    else:
        log.warning("Telegram failed: %s", resp.text)


def notify(service, changes: list):
    subject, body = _format_message(changes)
    try:
        send_email_via_api(service, subject, body)
    except Exception as exc:
        log.error("Email notification failed: %s", exc)
    send_telegram(body)

# ── Main: single run ─────────────────────────────────────────────────────────

def run(debug: bool = False):
    log.info("=== BSC checker starting (debug=%s) ===", debug)
    service   = get_gmail_service()
    old_state = load_state()

    try:
        with sync_playwright() as pw:
            page      = login_and_navigate(pw, service, debug=debug)
            new_state = extract_availability(page)
    except PlaywrightTimeout as exc:
        log.error("Playwright timeout: %s", exc)
        return
    except Exception as exc:
        log.error("Run failed: %s", exc, exc_info=True)
        return

    save_state(new_state)
    changes = detect_changes(old_state, new_state)
    if changes:
        log.info("Changes detected: %s", changes)
        notify(service, changes)
    else:
        log.info("No changes in preferred houses.")

    log.info("── Current status of preferred houses ──")
    for house in PREFERRED_HOUSES:
        count = new_state.get(house, 0)
        log.info("  %s %-32s %d available", "✅" if count > 0 else "  ", house, count)
    log.info("=== BSC checker done ===\n")


# ── Main: continuous loop (keeps browser alive between checks) ────────────────

def run_loop(check_interval_min: int = 25):
    """
    Keep a single browser session alive and check every check_interval_min minutes.
    Much faster than single-run mode: login only once (or when session expires).
    """
    service = get_gmail_service()
    log.info("=== BSC Housing Watcher — checking every %d min ===", check_interval_min)
    send_telegram(f"🔄 BSC Housing Watcher started (every {check_interval_min} min).")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page    = context.new_page()
        page.set_default_timeout(15_000)

        # ── Initial login ──────────────────────────────────────────────────────
        _do_full_login_on_page(page, service)
        dashboard_url = page.url
        old_state     = load_state()
        check_no      = 0

        while True:
            check_no += 1
            log.info("=== Check #%d ===", check_no)
            try:
                navigate_to_availability(page)
                new_state = extract_availability(page)
                save_state(new_state)

                changes = detect_changes(old_state, new_state)
                if changes:
                    notify(service, changes)
                    old_state = new_state
                else:
                    log.info("No changes.")

                log.info("── Preferred houses ──")
                for house in PREFERRED_HOUSES:
                    count = new_state.get(house, 0)
                    log.info("  %s %-32s %d", "✅" if count else "  ", house, count)

                log.info("Sleeping %d min until next check…", check_interval_min)
                time.sleep(check_interval_min * 60)

                # Return to dashboard via login URL (redirects if session still valid)
                page.goto(BSC_LOGIN_URL, wait_until="networkidle")
                page.wait_for_timeout(1500)
                if page.locator(SEL_FORGOT_PWD_LINK).is_visible():
                    log.info("Session expired — re-logging in…")
                    _do_full_login_on_page(page, service)

            except KeyboardInterrupt:
                log.info("Watcher stopped by user.")
                send_telegram("🛑 BSC Housing Watcher stopped.")
                break
            except Exception as exc:
                log.error("Check #%d failed: %s", check_no, exc, exc_info=True)
                log.info("Waiting 2 min before retry…")
                time.sleep(120)
                try:
                    page.goto(BSC_LOGIN_URL, wait_until="networkidle")
                    page.wait_for_timeout(1000)
                    if page.locator(SEL_FORGOT_PWD_LINK).is_visible():
                        _do_full_login_on_page(page, service)
                    dashboard_url = page.url
                except Exception as e2:
                    log.error("Recovery login failed: %s", e2)


def _do_full_login_on_page(page, service, debug: bool = False):
    """Perform full login on an existing page object (for loop mode re-login)."""
    global BSC_PASSWORD
    # Reload .env in case BSC_PASSWORD was updated
    load_dotenv(override=True)
    BSC_PASSWORD = os.environ.get("BSC_PASSWORD", "").strip()

    page.goto(BSC_LOGIN_URL, wait_until="networkidle")

    if BSC_PASSWORD:
        page.locator(SEL_USERNAME_FIELD).click()
        page.locator(SEL_USERNAME_FIELD).press_sequentially(BSC_USERNAME, delay=40)
        page.locator(SEL_PASSWORD_FIELD).click()
        page.locator(SEL_PASSWORD_FIELD).press_sequentially(BSC_PASSWORD, delay=40)
        page.locator(SEL_LOGIN_BTN).click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)
        if not page.locator(SEL_FORGOT_PWD_LINK).is_visible():
            _handle_password_change(page, service)
            return

    # Forgot-password flow
    _fill(page, SEL_USERNAME_FIELD, BSC_USERNAME)
    forgot_at = int(time.time())
    _click(page, SEL_FORGOT_PWD_LINK)
    page.wait_for_load_state("networkidle")
    temp_pwd = fetch_temp_password(service, since_ts=forgot_at)
    page.goto(BSC_LOGIN_URL, wait_until="networkidle")
    page.locator(SEL_USERNAME_FIELD).click()
    page.locator(SEL_USERNAME_FIELD).press_sequentially(BSC_USERNAME, delay=40)
    page.locator(SEL_PASSWORD_FIELD).click()
    page.locator(SEL_PASSWORD_FIELD).press_sequentially(temp_pwd, delay=40)
    page.locator(SEL_LOGIN_BTN).click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    _handle_password_change(page, service)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BSC Housing availability checker")
    parser.add_argument("--debug",    action="store_true", help="Visible browser + interactive pauses")
    parser.add_argument("--setup",    action="store_true", help="Run Gmail OAuth2 authorization only")
    parser.add_argument("--loop",     action="store_true", help="Run continuously (keeps browser alive)")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("CHECK_INTERVAL_MINUTES", 25)),
                        help="Minutes between checks in --loop mode (default: 25)")
    args = parser.parse_args()

    setup_logging(args.debug or args.setup)

    if args.setup:
        log.info("Running Gmail OAuth2 setup…")
        svc = get_gmail_service()
        log.info("Authorization successful. Token saved to %s", TOKEN_FILE)
        log.info("You can now run: python checker.py --loop")
    elif args.loop:
        run_loop(check_interval_min=args.interval)
    else:
        run(debug=args.debug)
