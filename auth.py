"""
Website sign-in support.

Credentials are kept per watched URL. Passwords go into the Windows Credential Manager
or the Mac Keychain (through the `keyring` package); if that is unavailable they are stored scrambled in the database.

Sign-in methods
  auto    try a normal web form, then a real (hidden) browser if Playwright is installed
  form    fill in the HTML login form and submit it
  basic   HTTP "Basic" sign-in (the browser's grey pop-up box)
  browser always use the hidden browser (for sites whose login page is built with JavaScript)
Sites that use a CAPTCHA or a one-time code (text/app) cannot be signed into automatically.
"""
import base64
import json
import os
import logging
import re
import sys
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

log = logging.getLogger("bidwatch")
KEYRING_SERVICE = "BidWatch"
LOGIN_URL_RE = re.compile(r"(log[-_]?in|sign[-_]?in|logon|/auth|/sso|/account/|/session|oauth|saml|"
                          r"/idp/|okta|adfs)", re.I)
USER_FIELD_RE = re.compile(r"(user|email|login|account|uid|name)", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    url_id INTEGER PRIMARY KEY,
    login_url TEXT DEFAULT '',
    username TEXT DEFAULT '',
    password_store TEXT DEFAULT '',   -- 'keyring' or scrambled text
    user_field TEXT DEFAULT '',
    pass_field TEXT DEFAULT '',
    method TEXT DEFAULT 'auto',
    status TEXT DEFAULT '',           -- needed | ok | failed
    detail TEXT DEFAULT '',
    last_login TEXT
);
"""


# ------------------------------------------------------------------ storage
def ensure_schema(db):
    db.conn.executescript(SCHEMA)
    db.conn.commit()


def vault_name():
    if sys.platform == "darwin":
        return "your Mac Keychain"
    if sys.platform == "win32":
        return "Windows Credential Manager"
    return "the system keyring"


def _keyring():
    try:
        import keyring
        keyring.get_keyring()
        return keyring
    except Exception:  # noqa: BLE001
        return None


def save_credentials(db, url_id, username, password, login_url="", method="auto", user_field="", pass_field=""):
    ensure_schema(db)
    if not login_url:  # keep the sign-in page address found earlier
        old = db.conn.execute("SELECT login_url FROM credentials WHERE url_id=?", (url_id,)).fetchone()
        login_url = old["login_url"] if old and old["login_url"] else ""
    kr = _keyring()
    store = "keyring"
    if kr:
        try:
            kr.set_password(KEYRING_SERVICE, f"url-{url_id}", password)
        except Exception:  # noqa: BLE001
            kr = None
    if not kr:
        store = "b64:" + base64.b64encode(password[::-1].encode()).decode()
    db.conn.execute(
        "INSERT OR REPLACE INTO credentials (url_id, login_url, username, password_store, user_field, pass_field, "
        "method, status, detail) VALUES (?,?,?,?,?,?,?, '', '')",
        (url_id, login_url, username, store, user_field, pass_field, method))
    db.conn.commit()


def _env_logins():
    """SITE_LOGINS secret: {"https://site/page": {"username": "...", "password": "...", "login_url": "..."}}.
    A key may also be just the website name (e.g. "vendors.example.gov") to cover every page on it."""
    raw = os.environ.get("SITE_LOGINS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {k.strip().lower().rstrip("/"): v for k, v in data.items() if isinstance(v, dict)}
    except (ValueError, AttributeError):
        log.warning("SITE_LOGINS is not valid JSON; website sign-ins skipped")
        return {}


def _env_credentials(url):
    logins = _env_logins()
    if not logins:
        return None
    u = url.strip().lower().rstrip("/")
    host = urlparse(u).netloc.removeprefix("www.")
    hit = logins.get(u) or logins.get(host) or logins.get("www." + host)
    if not hit or not hit.get("username"):
        return None
    return {"username": hit["username"], "password": hit.get("password", ""),
            "login_url": hit.get("login_url", ""), "method": hit.get("method", "auto"),
            "user_field": hit.get("user_field", ""), "pass_field": hit.get("pass_field", "")}


def get_credentials(db, url_id):
    ensure_schema(db)
    urow = db.conn.execute("SELECT url FROM urls WHERE id=?", (url_id,)).fetchone()
    if urow:
        env = _env_credentials(urow["url"])
        if env:
            return env
    row = db.conn.execute("SELECT * FROM credentials WHERE url_id=?", (url_id,)).fetchone()
    if not row or not row["username"]:
        return None
    cred = dict(row)
    store = row["password_store"] or ""
    if store == "keyring":
        kr = _keyring()
        cred["password"] = (kr.get_password(KEYRING_SERVICE, f"url-{url_id}") if kr else None) or ""
    elif store.startswith("b64:"):
        cred["password"] = base64.b64decode(store[4:]).decode()[::-1]
    else:
        cred["password"] = ""
    return cred


def delete_credentials(db, url_id):
    ensure_schema(db)
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(KEYRING_SERVICE, f"url-{url_id}")
        except Exception:  # noqa: BLE001
            pass
    db.conn.execute("DELETE FROM credentials WHERE url_id=?", (url_id,))
    db.conn.commit()


def set_status(db, url_id, status, detail="", login_url=None):
    ensure_schema(db)
    db.conn.execute("INSERT OR IGNORE INTO credentials (url_id) VALUES (?)", (url_id,))
    if login_url:
        db.conn.execute("UPDATE credentials SET login_url=COALESCE(NULLIF(login_url,''), ?) WHERE url_id=?",
                        (login_url, url_id))
    stamp = ", last_login=datetime('now','localtime')" if status == "ok" else ""
    db.conn.execute(f"UPDATE credentials SET status=?, detail=?{stamp} WHERE url_id=?", (status, detail, url_id))
    db.conn.commit()


def pending_logins(db):
    """Watched URLs waiting for the user to type in a username/password."""
    ensure_schema(db)
    return db.conn.execute(
        "SELECT u.id, u.url, u.label, c.login_url, c.status, c.detail FROM credentials c "
        "JOIN urls u ON u.id=c.url_id WHERE c.status IN ('needed','failed') AND u.active=1").fetchall()


# ------------------------------------------------------------------ detection
def _password_form(soup):
    if soup is None:
        return None
    pw = soup.find("input", attrs={"type": re.compile("^password$", re.I)})
    if not pw:
        return None
    return pw.find_parent("form") or soup


def needs_login(result, requested_url):
    """True when the response is a sign-in wall rather than the page we asked for."""
    if result.status == "401":
        return True
    final = result.final_url or requested_url
    redirected_to_login = (urlparse(final).path != urlparse(requested_url).path
                           and LOGIN_URL_RE.search(urlparse(final).path + "?" + urlparse(final).query or ""))
    if redirected_to_login:
        return True
    form = _password_form(result.soup)
    if form is None:
        return False
    # A page that is mostly a login box (not a big public page with a small sign-in box in the corner)
    if len(result.text) < 2500 or result.status == "403":
        return True
    return bool(LOGIN_URL_RE.search(urlparse(final).path))


def basic_auth_requested(result):
    return result.status == "401" and "basic" in result.headers.get("WWW-Authenticate",
                                                                   result.headers.get("www-authenticate", "")).lower()


# ------------------------------------------------------------------ signing in
def login(session, cred, cfg, target_url, first_result=None):
    """Sign in. Returns (ok, message, request_auth) — request_auth is set for Basic sign-in."""
    method = cred.get("method") or "auto"
    if not cred.get("password"):
        return False, "no password saved", None
    if method == "basic" or (method == "auto" and first_result is not None and basic_auth_requested(first_result)):
        return True, "signed in (basic)", (cred["username"], cred["password"])
    if method in ("auto", "form"):
        ok, msg = _form_login(session, cred, cfg, target_url, first_result)
        if ok or method == "form":
            return ok, msg, None
        log.info("  form sign-in did not work (%s); trying browser sign-in", msg)
    ok, msg = _browser_login(session, cred, cfg, target_url)
    return ok, msg, None


def _form_login(session, cred, cfg, target_url, first_result):
    login_url = cred.get("login_url") or ""
    if login_url:
        resp = session.get(login_url, timeout=cfg["request_timeout"])
        page_url, html = resp.url, resp.text
    elif first_result is not None and first_result.soup is not None:
        page_url, html = first_result.final_url or target_url, first_result.content.decode("utf-8", "ignore")
    else:
        resp = session.get(target_url, timeout=cfg["request_timeout"])
        page_url, html = resp.url, resp.text
    soup = BeautifulSoup(html, "html.parser")
    form = _password_form(soup)
    if form is None or form.name != "form":
        return False, "no sign-in form found on the page (it may need the browser method)"

    data, user_name, pass_name = {}, cred.get("user_field") or "", cred.get("pass_field") or ""
    text_inputs = []
    for inp in form.find_all(["input", "select", "textarea"]):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "text").lower()
        if typ in ("submit", "button", "image", "reset", "file"):
            continue
        if typ in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        if typ == "password" and not pass_name:
            pass_name = name
        if typ in ("text", "email") or (inp.name == "input" and typ == "tel"):
            text_inputs.append(name)
        data[name] = inp.get("value", "")
    if not user_name:
        guesses = [n for n in text_inputs if USER_FIELD_RE.search(n)]
        user_name = (guesses or text_inputs or [""])[0]
    if not (user_name and pass_name):
        return False, "could not tell which boxes are the username and password"
    data[user_name] = cred["username"]
    data[pass_name] = cred["password"]
    submit = form.find(["button", "input"], attrs={"type": re.compile("submit", re.I)})
    if submit is not None and submit.get("name"):
        data[submit["name"]] = submit.get("value", "")

    action = urljoin(page_url, form.get("action") or page_url)
    if (form.get("method") or "post").lower() == "get":
        resp = session.get(action, params=data, timeout=cfg["request_timeout"])
    else:
        resp = session.post(action, data=data, timeout=cfg["request_timeout"],
                            headers={"Referer": page_url, "Origin": f"{urlparse(page_url).scheme}://"
                                                                     f"{urlparse(page_url).netloc}"})
    after = BeautifulSoup(resp.text, "html.parser")
    if resp.status_code >= 400:
        return False, f"site answered HTTP {resp.status_code} to the sign-in"
    if _password_form(after) is not None:
        return False, "still on the sign-in page (wrong username/password, CAPTCHA, or extra security step)"
    return True, "signed in with web form"


def _browser_login(session, cred, cfg, target_url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, ("this site's sign-in needs a real browser; install it with "
                       "'pip install playwright' then 'playwright install chromium'")
    start = cred.get("login_url") or target_url
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=cfg["user_agent"])
            page = ctx.new_page()
            page.goto(start, wait_until="networkidle", timeout=60000)
            user_sel = (f'input[name="{cred["user_field"]}"]' if cred.get("user_field") else
                        'input[type="email"]:visible, input[type="text"]:visible')
            pass_sel = (f'input[name="{cred["pass_field"]}"]' if cred.get("pass_field") else
                        'input[type="password"]:visible')
            page.locator(user_sel).first.fill(cred["username"], timeout=15000)
            if page.locator(pass_sel).count() == 0:     # two-step sign-in: username, then password
                page.keyboard.press("Enter")
                page.wait_for_selector(pass_sel, timeout=20000)
            page.locator(pass_sel).first.fill(cred["password"])
            page.keyboard.press("Enter")
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            still = page.locator('input[type="password"]:visible').count() > 0
            for c in ctx.cookies():
                session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
            browser.close()
        if still:
            return False, "still on the sign-in page (wrong username/password, CAPTCHA, or extra security step)"
        return True, "signed in with browser"
    except Exception as e:  # noqa: BLE001
        return False, f"browser sign-in failed: {e.__class__.__name__}"
