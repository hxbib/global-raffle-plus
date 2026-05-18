
import asyncio
import contextlib
import email
import imaplib
import logging
import os
import random
import re
import sqlite3
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from camoufox.async_api import AsyncCamoufox


env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

IMAP_HOST      = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT      = int(os.getenv("IMAP_PORT", "993"))
IMAP_USERNAME  = os.getenv("IMAP_USERNAME", "")
IMAP_PASSWORD  = os.getenv("IMAP_PASSWORD", "")
IMAP_FOLDER    = os.getenv("IMAP_FOLDER", "INBOX")

SEARCH_WINDOW_HOURS = int(os.getenv("SEARCH_WINDOW_HOURS", "48"))
ONLY_UNSEEN         = os.getenv("ONLY_UNSEEN", "true").lower() in ("true", "1", "yes")

REQUIRED_FROM_EMAIL = "reply@globalcitizen.org"
REQUIRED_SUBJECT    = "Confirm your email"

WORKERS         = int(os.getenv("WORKERS", "10"))
HEADLESS        = os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes")
HUMANIZE_CURSOR = os.getenv("HUMANIZE_CURSOR", "true").lower() in ("true", "1", "yes")

NAV_TIMEOUT_MS    = int(os.getenv("NAV_TIMEOUT_MS", "25000"))
RESPONSE_TIMEOUT  = int(os.getenv("RESPONSE_TIMEOUT", "20000"))

RETRY_ON_400_MAX           = 3
ROTATE_ON_429              = True
MAX_TOTAL_RETRIES_PER_LINK = 6

ROOT_DIR     = Path(__file__).resolve().parent
PROXIES_PATH = ROOT_DIR / "proxies.txt"
DB_PATH      = ROOT_DIR / "confirmedLinks.sqlite3"
LINKS_LOG    = ROOT_DIR / "linksFound.txt"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("confirm-links")

US_TIMEZONE   = "America/New_York"
US_LOCALE     = "en-US"
US_ACCEPTLANG = "en-US,en;q=0.9"

CONFIRM_TEXT_VARIANTS = (
    "confirm your email",
    "confirm your account",
    "verify your email",
    "verify email",
)
IGNORE_TEXT_PATTERNS = (
    "unsubscribe",
    "manage email preferences",
    "view email in browser",
    "google play",
    "app store",
    "facebook",
    "instagram",
    "tiktok",
    "youtube",
    "text us",
)



def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS visited_links (
            url TEXT PRIMARY KEY,
            first_seen_ts INTEGER NOT NULL,
            last_attempt_ts INTEGER,
            success INTEGER DEFAULT 0,
            last_status INTEGER
        )
    """)
    conn.commit()
    return conn

def _now_ts() -> int:
    return int(time.time())

def _decode_header_value(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""

def _message_date_within(msg: email.message.Message, hours: int) -> bool:
    try:
        datestr = msg.get("Date")
        if not datestr:
            return True
        dt = email.utils.parsedate_to_datetime(datestr)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) <= timedelta(hours=hours)
    except Exception:
        return True

def _sender_ok(msg: email.message.Message) -> bool:
    frm = (msg.get("From") or "")
    return REQUIRED_FROM_EMAIL.lower() in frm.lower()

def _subject_ok(msg: email.message.Message) -> bool:
    subj = _decode_header_value(msg.get("Subject") or "")
    return subj.strip().lower() == REQUIRED_SUBJECT.lower()

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _score_anchor(a) -> int:
    score = 0
    txt = _norm(a.get_text(" ", strip=True))
    href = (a.get("href") or "").strip()
    role = (a.get("role") or "").lower()
    style = (a.get("style") or "").lower()
    cls = " ".join(a.get("class", [])).lower()

    if any(k in txt for k in CONFIRM_TEXT_VARIANTS):
        score += 10
    if "confirm" in href.lower():
        score += 6

    if role == "button":
        score += 3
    if "button" in cls:
        score += 2
    if ("background" in style) or ("border-radius" in style) or ("padding" in style):
        score += 1

    p = a.find_parent()
    if p:
        with contextlib.suppress(Exception):
            ptxt = _norm(p.get_text(" ", strip=True))[:300]
            if any(k in ptxt for k in CONFIRM_TEXT_VARIANTS):
                score += 2

    if any(bad in txt for bad in IGNORE_TEXT_PATTERNS):
        score -= 10

    if "globalcitizen.org" in href.lower():
        score += 1

    return score

def _extract_confirm_button_links(msg: email.message.Message) -> List[str]:
    candidates: List[str] = []

    def extract_from_html(html: str):
        soup = BeautifulSoup(html, "lxml")
        anchors = list(soup.find_all("a", href=True))
        if not anchors:
            return

        scored = []
        for a in anchors:
            href = a["href"]
            if not href.lower().startswith(("http://", "https://")):
                continue
            score = _score_anchor(a)
            if score >= 1 or ("confirm" in href.lower()):
                scored.append((score, href))

        if not scored:
            return

        scored.sort(key=lambda t: t[0], reverse=True)
        top_score, top_href = scored[0]

        if top_score < 6:
            same_domain = [h for s, h in scored if "globalcitizen.org" in h.lower() and "confirm" in h.lower()]
            if same_domain:
                top_href = same_domain[0]

        candidates.append(top_href)

    def extract_from_text(text: str):
        for m in re.finditer(r"https?://[^\s\"'<>]+", text, flags=re.IGNORECASE):
            u = m.group(0)
            if "confirm" in u.lower():
                candidates.append(u)

    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            if ctype in ("text/html", "text/plain"):
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                text = payload.decode("utf-8", errors="ignore")
                if ctype == "text/html":
                    extract_from_html(text)
                else:
                    extract_from_text(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode("utf-8", errors="ignore")
            extract_from_html(text)
            if not candidates:
                extract_from_text(text)

    seen = set()
    for u in candidates:
        if u not in seen:
            seen.add(u)
            return [u]
    return []

def fetch_links_from_gmail() -> List[str]:
    if not IMAP_USERNAME or not IMAP_PASSWORD:
        log.error("IMAP_USERNAME / IMAP_PASSWORD not set. Put them in .env (use a Google App Password).")
        sys.exit(1)

    log.info("IMAP: connecting %s:%d", IMAP_HOST, IMAP_PORT)
    ctx = ssl.create_default_context()
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx) as M:
            typ, login_data = M.login(IMAP_USERNAME, IMAP_PASSWORD)
            if typ != "OK":
                log.error("IMAP login failed: %s", login_data)
                sys.exit(1)

            typ, _ = M.select(f'"{IMAP_FOLDER}"', readonly=True)
            if typ != "OK":
                log.error("IMAP select failed for folder %s", IMAP_FOLDER)
                sys.exit(1)

            since = (datetime.now() - timedelta(hours=SEARCH_WINDOW_HOURS)).strftime("%d-%b-%Y")
            criteria = [
                'UNSEEN' if ONLY_UNSEEN else 'ALL',
                'FROM', f'"{REQUIRED_FROM_EMAIL}"',
                'SUBJECT', f'"{REQUIRED_SUBJECT}"',
                'SINCE', since
            ]
            typ, data = M.search(None, *criteria)
            if typ != "OK":
                log.error("IMAP search failed with criteria: %s", criteria)
                sys.exit(1)

            ids = data[0].split() if data and data[0] else []
            log.info("IMAP: %d message(s) matched", len(ids))

            all_links: List[str] = []
            for msg_id in reversed(ids):
                typ, msg_data = M.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                if not _message_date_within(msg, SEARCH_WINDOW_HOURS):
                    continue
                if not (_sender_ok(msg) and _subject_ok(msg)):
                    continue
                link = _extract_confirm_button_links(msg)
                if link:
                    all_links.extend(link)

            deduped = []
            seen = set()
            for u in all_links:
                if u not in seen:
                    seen.add(u)
                    deduped.append(u)

            log.info("IMAP: extracted %d targeted link(s)", len(deduped))
            if deduped:
                LINKS_LOG.write_text("\n".join(deduped))
                log.info("Wrote links to %s", LINKS_LOG)
            return deduped
    except imaplib.IMAP4.error as e:
        log.error("IMAP error: %s", e)
        log.error("Tip: Enable IMAP in Gmail and use a Google App Password (2FA required).")
        sys.exit(1)



@dataclass(frozen=True)
class ProxyEntry:
    raw: str
    playwright_dict: Optional[dict]

def load_proxies(path: Path) -> List[ProxyEntry]:
    if not path.exists():
        log.warning("proxies.txt not found; defaulting to 'none'")
        return [ProxyEntry("none", None)]

    entries: List[ProxyEntry] = []
    for line in path.read_text().splitlines():
        p = line.strip()
        if not p or p.startswith("#"):
            continue
        if p.lower() == "none":
            entries.append(ProxyEntry("none", None))
            continue
        server = p if "://" in p else f"http://{p}"
        entries.append(ProxyEntry(p, {"server": server}))
    return entries or [ProxyEntry("none", None)]



class CamouBrowserPool:
    def __init__(self, proxies: List[ProxyEntry]):
        self._proxies = proxies
        self._browsers: Dict[ProxyEntry, AsyncCamoufox] = {}
        self._launched: Dict[ProxyEntry, bool] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        for browser in list(self._browsers.values()):
            with contextlib.suppress(Exception):
                await browser.close()
        self._browsers.clear()

    def choose_proxy(self) -> ProxyEntry:
        return random.choice(self._proxies)

    async def get_browser(self, proxy: ProxyEntry) -> AsyncCamoufox:
        if proxy in self._browsers and self._launched.get(proxy):
            return self._browsers[proxy]
        browser = AsyncCamoufox(
            proxy=proxy.playwright_dict,
            geoip=True,
            headless=HEADLESS,
            humanize=HUMANIZE_CURSOR,
        )
        await browser.__aenter__()
        self._browsers[proxy] = browser
        self._launched[proxy] = True
        return browser



@dataclass
class LinkTask:
    url: str
    attempts: int = 0

class LinkConfirmer:
    def __init__(self, db_conn: sqlite3.Connection, pool: CamouBrowserPool):
        self.db = db_conn
        self.pool = pool

    def _already_success(self, url: str) -> bool:
        cur = self.db.execute("SELECT success FROM visited_links WHERE url=?", (url,))
        row = cur.fetchone()
        return bool(row and row[0])

    def _ensure_row(self, url: str):
        self.db.execute(
            "INSERT OR IGNORE INTO visited_links (url, first_seen_ts) VALUES (?, ?)",
            (url, _now_ts()),
        )
        self.db.commit()

    def _update_attempt(self, url: str, success: bool, status: Optional[int]):
        self.db.execute(
            "UPDATE visited_links SET last_attempt_ts=?, success=?, last_status=? WHERE url=?",
            (_now_ts(), int(success), status if status is not None else None, url),
        )
        self.db.commit()

    @staticmethod
    def _has_success_sentence(text: str) -> bool:
        sentences = re.split(r"[.!?]\s+", text, flags=re.MULTILINE)
        for s in sentences:
            ls = s.lower()
            if all(word in ls for word in ("address", "confirmed")):
                return True
        return False

    @staticmethod
    def _random_ua() -> str:
        UAS = [
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
        ]
        return random.choice(UAS)

    @staticmethod
    def _random_us_coords() -> dict:
        lat = random.uniform(25.0, 49.0)
        lon = random.uniform(-124.0, -66.0)
        return {"latitude": lat, "longitude": lon}

    async def run(self, urls: Iterable[str]):
        q: asyncio.Queue[Union[LinkTask, None]] = asyncio.Queue()
        seen = set()
        enqueued = 0

        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            self._ensure_row(u)
            if self._already_success(u):
                log.info("SKIP (already confirmed): %s", u)
                continue
            await q.put(LinkTask(u))
            enqueued += 1

        if enqueued == 0:
            log.info("No pending links to confirm.")
            return

        log.info("Queue size: %d link(s)", enqueued)

        async def worker(wid: int):
            while True:
                task = await q.get()
                if task is None:
                    q.task_done()
                    break
                await self._process_one(wid, task, q)
                q.task_done()

        workers = [asyncio.create_task(worker(i + 1)) for i in range(WORKERS)]

        for _ in range(WORKERS):
            await q.put(None)

        await q.join()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _process_one(self, wid: int, task: LinkTask, q: asyncio.Queue):
        url = task.url
        task.attempts += 1

        proxy = self.pool.choose_proxy()
        browser = await self.pool.get_browser(proxy)

        us_geo = self._random_us_coords()

        context = await browser.new_context(
            user_agent=self._random_ua(),
            locale=US_LOCALE,
            timezone_id=US_TIMEZONE,
            geolocation=us_geo,
            permissions=["geolocation"],
            extra_http_headers={"Accept-Language": US_ACCEPTLANG},
            java_script_enabled=True,
        )
        page = await context.new_page()

        status_holder = {"status": None}
        async def on_response(resp):
            if resp.request.resource_type == "document":
                with contextlib.suppress(Exception):
                    status_holder["status"] = resp.status
        page.on("response", on_response)

        success = False
        try:
            page.set_default_timeout(NAV_TIMEOUT_MS)
            page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

            resp = await page.goto(url, wait_until="domcontentloaded")
            if resp is not None:
                status_holder["status"] = resp.status

            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=RESPONSE_TIMEOUT)

            body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            if self._has_success_sentence(body_text):
                success = True
                log.info("[W%02d] ✅ Confirmed: %s", wid, url)
            else:
                await page.wait_for_timeout(1500)
                body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                if self._has_success_sentence(body_text):
                    success = True
                    log.info("[W%02d] ✅ Confirmed (delayed): %s", wid, url)

            self._update_attempt(url, success, status_holder["status"])

            if not success:
                code = status_holder["status"]
                if code == 400 and task.attempts < RETRY_ON_400_MAX:
                    log.warning("[W%02d] 400 received; retrying %d/%d: %s",
                                wid, task.attempts, RETRY_ON_400_MAX, url)
                    await q.put(LinkTask(url))
                elif code == 429 and ROTATE_ON_429 and task.attempts < MAX_TOTAL_RETRIES_PER_LINK:
                    log.warning("[W%02d] 429 rate-limited; rotating and retrying %d/%d: %s",
                                wid, task.attempts, MAX_TOTAL_RETRIES_PER_LINK, url)
                    await q.put(LinkTask(url))
                elif code is None and task.attempts < 2:
                    log.warning("[W%02d] No status observed; quick retry: %s", wid, url)
                    await q.put(LinkTask(url))
                else:
                    log.error("[W%02d] ❌ Not confirmed after attempt %d (status=%s): %s",
                              wid, task.attempts, code, url)
        except Exception:
            self._update_attempt(url, False, None)
            if task.attempts < MAX_TOTAL_RETRIES_PER_LINK:
                log.exception("[W%02d] Exception; requeueing (attempt %d/%d): %s",
                              wid, task.attempts, MAX_TOTAL_RETRIES_PER_LINK, url)
                await q.put(LinkTask(url))
            else:
                log.exception("[W%02d] Exception; giving up: %s", wid, url)
        finally:
            with contextlib.suppress(Exception):
                await page.close()
                await context.close()



async def main():
    if not IMAP_USERNAME or not IMAP_PASSWORD:
        log.error("IMAP_USERNAME / IMAP_PASSWORD not set in .env (use a Google App Password).")
        sys.exit(1)

    urls = fetch_links_from_gmail()
    if not urls:
        log.info("No links found. Exiting.")
        return

    proxies = load_proxies(PROXIES_PATH)
    log.info("Loaded %d proxy entry/entries", len(proxies))

    db = _init_db()
    t0 = time.time()

    async with CamouBrowserPool(proxies) as pool:
        confirmer = LinkConfirmer(db, pool)
        await confirmer.run(urls)

    elapsed = time.time() - t0
    cur = db.execute("SELECT COUNT(*) FROM visited_links WHERE success=1")
    confirmed = cur.fetchone()[0]
    cur = db.execute("SELECT COUNT(*) FROM visited_links")
    total = cur.fetchone()[0]
    log.info("Done in %.1fs | Confirmed %d/%d (DB totals)", elapsed, confirmed, total)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
