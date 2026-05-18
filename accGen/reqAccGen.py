
import os
import sys
import time
import json
import csv
import re
import random
import logging
import traceback
import threading
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
CSV_INPUT = ROOT / "data" / "accounts.csv"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = ROOT / "logs"
PROXY_LIST_PATH = ROOT / "proxies.txt"

RUN_STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
SUCCESS_PATH = OUTPUT_DIR / "success.csv"
FAILED_PATH = OUTPUT_DIR / "failed.csv"

for p in [OUTPUT_DIR, LOG_DIR]:
    p.mkdir(parents=True, exist_ok=True)

BASE_WEB = "https://www.globalcitizen.org"
BASE_API = "https://api.globalcitizen.org"
WAIT_SEC = 35
PAGE_PAUSE = 0.4
MAX_USERNAME_TRIES = 7
DEFAULT_TIMEOUT = 20

EMAIL_ALREADY_REGISTERED_PHRASE = "this email is already registered. sign in to take action."
USERNAME_IN_USE_PHRASE = "that username is already in use."
BLOCK_PHRASES = [
    "new signups from this ip address have been temporarily disabled",
    "access denied",
    "request blocked",
    "temporarily blocked",
    "too many requests",
    "rate limit",
    "403",
    "429",
    "unusual traffic",
    "challenge required",
    "your request looks automated",
    "blocked due to",
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)

ANALYTICS_UA_BRANDS = [
    {"brand": "Not)A;Brand", "version": "8"},
    {"brand": "Chromium", "version": "139"},
    {"brand": "Google Chrome", "version": "139"},
]

log_path = LOG_DIR / f"run-{RUN_STAMP}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(log_path), encoding="utf-8"),
    ],
)

for name in ["urllib3", "requests", "charset_normalizer", "h11", "hpack"]:
    logging.getLogger(name).setLevel(logging.WARNING)

SUCCESS_FIELDS = ["timestamp", "Email", "Password", "First Name", "Last Name", "Username", "Zipcode"]
FAILED_FIELDS = SUCCESS_FIELDS

FILE_LOCK = threading.Lock()
SETS_LOCK = threading.Lock()
HALT_EVENT = threading.Event()

def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

def append_row(csv_path: Path, fieldnames: List[str], row: Dict[str, str]):
    with FILE_LOCK:
        file_exists = csv_path.exists()
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            sanitized = {k: row.get(k, "") for k in fieldnames}
            writer.writerow(sanitized)

def load_success_sets(success_path: Path) -> Tuple[Set[str], Set[str]]:
    emails, usernames = set(), set()
    if success_path.exists():
        try:
            with open(success_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    e = (r.get("Email") or "").strip()
                    u = (r.get("Username") or "").strip()
                    if e:
                        emails.add(e.lower())
                    if u:
                        usernames.add(u.lower())
        except Exception as exc:
            logging.warning(f"Could not read existing {success_path.name}: {exc}")
    logging.info(f"Loaded {len(emails)} successful emails and {len(usernames)} usernames from {success_path.name}")
    return emails, usernames

_PROXY_RE_FULL = re.compile(
    r"^(?:(?P<scheme>https?|socks5h?)://)?"
    r"(?:(?P<user>[^:@]+):(?P<pw>[^@]+)@)?"
    r"(?P<host>[^:]+):(?P<port>\d+)$",
    re.IGNORECASE,
)

def parse_proxy_line(line: str) -> Optional[Dict[str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) == 4 and "://" not in line:
        host, port, user, pw = parts
        scheme = "http"
        return {
            "scheme": scheme, "host": host, "port": port,
            "user": user, "pw": pw,
            "url_no_auth": f"{scheme}://{host}:{port}",
            "url_with_auth": f"{scheme}://{user}:{pw}@{host}:{port}",
            "raw": line,
        }
    m = _PROXY_RE_FULL.match(line)
    if not m:
        return None
    d = m.groupdict()
    scheme = (d.get("scheme") or "http").lower()
    host = d["host"]
    port = d["port"]
    user = d.get("user") or ""
    pw = d.get("pw") or ""
    proxy_url_no_auth = f"{scheme}://{host}:{port}"
    proxy_url_with_auth = f"{scheme}://{user}:{pw}@{host}:{port}" if user and pw else ""
    return {
        "scheme": scheme, "host": host, "port": port,
        "user": user, "pw": pw,
        "url_no_auth": proxy_url_no_auth,
        "url_with_auth": proxy_url_with_auth,
        "raw": line,
    }

def load_proxies(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        logging.error(f"Proxy list not found at {path}. Please create proxies.txt.")
        sys.exit(1)
    proxies: List[Dict[str, str]] = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            p = parse_proxy_line(ln)
            if p:
                proxies.append(p)
    if not proxies:
        logging.error("No valid proxies parsed from proxies.txt.")
        sys.exit(1)
    logging.info(f"Loaded {len(proxies)} proxies.")
    return proxies

def make_requests_proxies(proxy: Dict[str, str]) -> Dict[str, str]:
    url = proxy["url_with_auth"] or proxy["url_no_auth"]
    return {"http": url, "https": url}

class EnvironmentBlockedError(Exception):
    pass

def new_session(proxy: Optional[Dict[str, str]] = None) -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1",
        "Origin": BASE_WEB,
        "Referer": f"{BASE_WEB}/en/",
    })
    retries = Retry(
        total=3, backoff_factor=0.4,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)

    if proxy:
        sess.proxies.update(make_requests_proxies(proxy))

    return sess

def get_csrf(sess: requests.Session) -> str:
    sess.get(f"{BASE_WEB}/en/", timeout=DEFAULT_TIMEOUT)
    csrf = sess.cookies.get("prod_csrftoken") or sess.cookies.get("csrftoken") or ""
    return csrf

def is_block_response(resp: requests.Response) -> bool:
    try:
        text = (resp.text or "").lower()
    except Exception:
        text = ""
    return (resp.status_code in (403, 429)) or any(k in text for k in BLOCK_PHRASES)

def api_validate_email(sess: requests.Session, email: str) -> Dict:
    url = f"{BASE_API}/v1/emails/validate/"
    params = {"email": email}
    headers = {
        "Accept": "*/*",
        "x-requested-with": "XMLHttpRequest",
        "apikey": "7292e0560c6b44258c66fd60f0a19a32",
        "Referer": f"{BASE_WEB}/",
    }
    r = sess.get(url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
    if is_block_response(r):
        raise EnvironmentBlockedError("Blocked on validate_email")
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "body": r.text[:400]}

def api_register_shadow(sess: requests.Session, csrf: str, payload: Dict) -> requests.Response:
    url = f"{BASE_WEB}/api/users/register_shadow/"
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json",
        "x-csrftoken": csrf or "",
        "x-requested-with": "XMLHttpRequest",
        "Referer": f"{BASE_WEB}/en/",
    }
    r = sess.post(url, headers=headers, data=json.dumps(payload), timeout=DEFAULT_TIMEOUT)
    return r

def api_set_password(sess: requests.Session, csrf: str, password: str) -> requests.Response:
    url = f"{BASE_WEB}/api/users/set_password/"
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json",
        "x-csrftoken": csrf or "",
        "x-requested-with": "XMLHttpRequest",
        "Referer": f"{BASE_WEB}/en/",
    }
    data = {
        "password1": password,
        "password2": password,
        "user_code": "",
        "emailOptIn": False,
    }
    r = sess.post(url, headers=headers, data=json.dumps(data), timeout=DEFAULT_TIMEOUT)
    return r

def api_track(sess: requests.Session, csrf: str, event_name: str, auth_type: str):
    url = f"{BASE_API}/v1/analytics/track/"
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json; charset=UTF-8",
        "x-csrftoken": csrf or "",
        "x-requested-with": "XMLHttpRequest",
        "Referer": f"{BASE_WEB}/",
    }

    def now_iso_z():
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    payload = {
        "context": {
            "library": {"name": "analytics.js", "version": "1.0"},
            "campaign": {"name": None, "source": None, "medium": None, "term": None, "content": None},
            "locale": "en-US",
            "page": {
                "path": "/en/",
                "referrer": f"{BASE_WEB}/en/",
                "search": "",
                "title": "Global Citizen - Join the Movement Changing the World",
                "url": f"{BASE_WEB}/en/",
            },
            "screenHeight": 1112,
            "screenWidth": 1710,
            "userAgent": UA,
            "userAgentData": {"brands": ANALYTICS_UA_BRANDS, "mobile": False, "platform": "macOS"},
        },
        "sentAt": now_iso_z(),
        "timestamp": now_iso_z(),
        "originalTimestamp": now_iso_z(),
        "eventName": "Event User Auth",
        "authType": auth_type,
        "provider": "email",
        "method": "web",
        "elementName": "Sign up modal",
        "isReferral": False,
        "emailOptIn": False,
        "campaignId": None,
        "timezone": "America/New_York",
        "messageId": f"ajs-next-{event_name}-test",
    }
    try:
        sess.post(url, headers=headers, data=json.dumps(payload), timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass

def username_taken_in_body(text: str) -> bool:
    return USERNAME_IN_USE_PHRASE in (text or "").lower()

def email_registered_in_body(text: str) -> bool:
    return EMAIL_ALREADY_REGISTERED_PHRASE in (text or "").lower()

def gen_fallback_username(prefix: str = "defaultprefix") -> str:
    return f"{prefix}{random.randint(10000, 99999)}"

def create_account_via_requests(row: Dict[str, str]) -> str:
    email = row.get("Email", "").strip()
    password = row.get("Password", "").strip()
    first_name = row.get("First Name", "").strip()
    last_name = row.get("Last Name", "").strip()
    username = row.get("Username", "").strip()
    zipcode = row.get("Zipcode", "").strip()

    logging.info(f"Starting (requests) account for {email} / {username}")

    sess = requests.Session()
    return_username = username

    raise RuntimeError("caller must pass a session")

def make_flow_runner(sess: requests.Session):
    def run(row: Dict[str, str]) -> str:
        email = row.get("Email", "").strip()
        password = row.get("Password", "").strip()
        first_name = row.get("First Name", "").strip()
        last_name = row.get("Last Name", "").strip()
        username = row.get("Username", "").strip()
        zipcode = row.get("Zipcode", "").strip()

        csrf = get_csrf(sess)

        try:
            _val = api_validate_email(sess, email)
        except EnvironmentBlockedError:
            raise
        except Exception:
            _val = {}

        minimal_payload = {
            "email": email,
            "emailOptIn": False,
            "sourceUrl": f"{BASE_WEB}/en/",
            "user_code": "",
            "referral_code": None,
        }
        r = api_register_shadow(sess, csrf, minimal_payload)
        if is_block_response(r):
            raise EnvironmentBlockedError("Blocked on register_shadow (minimal)")

        if r.status_code >= 400:
            body = (r.text or "")
            if email_registered_in_body(body):
                logging.info("Email already registered → treating as SUCCESS per policy.")
                return username

        time.sleep(PAGE_PAUSE)

        api_track(sess, csrf, event_name="shadow-signup", auth_type="Shadow User Sign Up")

        current_username = username
        attempts = 0
        while True:
            attempts += 1
            details_payload = {
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "country": "US",
                "postal_code": zipcode,
                "profile_slug": current_username,
                "public_profile_enabled": False,
                "user_code": "",
                "referral_code": None,
            }
            r2 = api_register_shadow(sess, csrf, details_payload)
            if is_block_response(r2):
                raise EnvironmentBlockedError("Blocked on register_shadow (details)")

            if 200 <= r2.status_code < 300:
                break

            body = (r2.text or "")
            if username_taken_in_body(body) or "profile_slug" in body.lower():
                logging.info(f"Username taken: {current_username!r}. Generating a fallback…")
                if attempts >= MAX_USERNAME_TRIES:
                    raise RuntimeError("Exceeded max attempts to find a free username")
                current_username = gen_fallback_username("prefixdefault")
                continue

            if email_registered_in_body(body):
                logging.info("Email already registered (details) → treating as SUCCESS per policy.")
                return username

            raise RuntimeError(f"register_shadow(details) failed: {r2.status_code} {body[:300]}")

        time.sleep(PAGE_PAUSE)

        r3 = api_set_password(sess, csrf, password)
        if is_block_response(r3):
            raise EnvironmentBlockedError("Blocked on set_password")

        if not (200 <= r3.status_code < 300):
            body = (r3.text or "")
            if email_registered_in_body(body):
                logging.info("Email already registered (password) → treating as SUCCESS per policy.")
                return current_username
            raise RuntimeError(f"set_password failed: {r3.status_code} {body[:300]}")

        api_track(sess, csrf, event_name="signup", auth_type="Sign Up")

        return current_username
    return run

def worker_process_account(row_idx: int,
                           row_dict: Dict[str, str],
                           proxies_master: Optional[List[Dict[str, str]]],
                           success_emails: Set[str],
                           success_usernames: Set[str],
                           no_proxy: bool = False) -> None:
    if HALT_EVENT.is_set():
        return

    row_dict_with_ts = {"timestamp": now_iso(), **row_dict}
    proxies_pool = [] if no_proxy else (proxies_master[:] if proxies_master else [])
    if not no_proxy:
        random.shuffle(proxies_pool)

    account_done = False

    def _attempt_with(sess_builder):
        nonlocal account_done
        sess = None
        try:
            sess = sess_builder()
            runner = make_flow_runner(sess)
            final_username = runner(row_dict)

            if final_username and final_username != row_dict.get("Username", ""):
                row_dict["Username"] = final_username
                row_dict_with_ts["Username"] = final_username

            append_row(SUCCESS_PATH, SUCCESS_FIELDS, row_dict_with_ts)

            with SETS_LOCK:
                e = row_dict.get("Email", "").strip().lower()
                u = row_dict.get("Username", "").strip().lower()
                if e: success_emails.add(e)
                if u: success_usernames.add(u)

            account_done = True
            logging.info(f"✅ SUCCESS (requests) for {row_dict.get('Email')} (username: {row_dict.get('Username')})")
        finally:
            if sess:
                try:
                    sess.close()
                except Exception:
                    pass

    if no_proxy:
        logging.info("No-proxy mode: using direct requests session…")
        try:
            _attempt_with(lambda: new_session(None))
        except EnvironmentBlockedError as eblock:
            logging.error(f"Blocked without proxy: {eblock}")
        except Exception as e:
            logging.error(f"Direct session failed: {e}")
            logging.debug(traceback.format_exc())
    else:
        while proxies_pool and not account_done and not HALT_EVENT.is_set():
            proxy = proxies_pool.pop(0)
            logging.info(f"Using proxy: {proxy['raw']}")
            try:
                _attempt_with(lambda p=proxy: new_session(p))
            except EnvironmentBlockedError as eblock:
                logging.error(f"Proxy blocked; rotating. Details: {eblock}")
            except requests.RequestException as rexc:
                logging.error(f"Network error; rotating. Details: {rexc}")
            except Exception as e:
                logging.error(f"Unexpected error; rotating. Details: {e}")
                logging.debug(traceback.format_exc())
            if not account_done and proxies_pool and not HALT_EVENT.is_set():
                time.sleep(0.8)

    if not account_done:
        logging.error("All attempts exhausted for this account. Halting the run as requested.")
        append_row(FAILED_PATH, FAILED_FIELDS, row_dict_with_ts)
        HALT_EVENT.set()

def main():
    parser = argparse.ArgumentParser(description="GlobalCitizen account creator (requests-based)")
    parser.add_argument("--workers", type=int, default=50, help="Number of parallel workers. Default: 50")
    parser.add_argument("--no-proxy", action="store_true", help="Run without proxies.")
    args = parser.parse_args()

    if not CSV_INPUT.exists():
        logging.error(f"Input CSV not found at {CSV_INPUT}. Please place your file there.")
        sys.exit(1)

    df = pd.read_csv(CSV_INPUT)
    required_cols = ["Email", "Password", "First Name", "Last Name", "Username", "Zipcode"]
    for c in required_cols:
        if c not in df.columns:
            logging.error(f"Missing required column: {c}")
            sys.exit(1)

    success_emails, success_usernames = load_success_sets(SUCCESS_PATH)

    def already_done(row) -> bool:
        e = ("" if pd.isna(row.get("Email")) else str(row["Email"]).strip().lower())
        u = ("" if pd.isna(row.get("Username")) else str(row["Username"]).strip().lower())
        return (e in success_emails) or (u in success_usernames)

    initial_count = len(df)
    df = df[~df.apply(already_done, axis=1)].reset_index(drop=True)
    skipped = initial_count - len(df)
    if skipped > 0:
        logging.info(f"Skipping {skipped} rows already present in success.csv")

    if len(df) == 0:
        logging.info("Nothing to do. All rows already completed.")
        return

    no_proxy = bool(args.no_proxy)
    proxies_master = None
    if not no_proxy:
        proxies_master = load_proxies(PROXY_LIST_PATH)

    max_workers = max(1, int(args.workers))
    logging.info(f"Starting with up to {max_workers} parallel workers… (no_proxy={no_proxy})")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for idx, row in df.iterrows():
            if HALT_EVENT.is_set():
                break
            row_dict = {k: ("" if pd.isna(v) else str(v)) for k, v in row.to_dict().items()}
            fut = executor.submit(
                worker_process_account,
                idx, row_dict, proxies_master, success_emails, success_usernames, no_proxy
            )
            futures.append(fut)

        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                logging.error(f"Worker crashed: {e}")
                logging.debug(traceback.format_exc())

    logging.info(f"Run complete. Rolling files updated → {SUCCESS_PATH.name}, {FAILED_PATH.name}")

if __name__ == "__main__":
    main()
