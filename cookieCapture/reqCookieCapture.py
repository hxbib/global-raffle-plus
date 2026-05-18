
import argparse
import csv
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple

import requests

ROOT = Path(__file__).resolve().parent
COOKIES_ROOT = ROOT / "accountCookies"
LOG_ROOT = ROOT / "logs"
ACCOUNTS_CSV = ROOT / "accountInfo.csv"
PROXIES_TXT = ROOT / "proxies.txt"
ERRORS_CSV = LOG_ROOT / "errors.csv"

SITE_ORIGIN = "https://www.globalcitizen.org"
BASE_URL = f"{SITE_ORIGIN}/en/"
LOGIN_URL = "https://api.globalcitizen.org/v1/me/login/"
PROFILE_URL = f"{SITE_ORIGIN}/api/users/profile/"

LOGIN_ERROR_TEXT = "Unable to log in with provided credentials."

DEFAULT_MAX_RETRIES = 3
DEFAULT_WORKERS = 50
REQUEST_TIMEOUT = 30
MIN_VALID_BYTES = 120

LOGGING_LOCK = Lock()
TOTAL_ACCOUNTS = 0

def set_total_accounts(n: int):
    global TOTAL_ACCOUNTS
    TOTAL_ACCOUNTS = n

def setup_logging(verbose: bool) -> logging.Logger:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cookieCapture")
    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    fh = logging.FileHandler(LOG_ROOT / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)
    return logger

def safe_log(logger: logging.Logger, level: str, message: str):
    with LOGGING_LOCK:
        getattr(logger, level)(message)

def sanitize_filename(s: str) -> str:
    return re.sub(r"[^\w\-.]+", "_", s)

def append_error_row(row: Dict[str, str]):
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    is_new = not ERRORS_CSV.exists()
    with ERRORS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "email", "attempt", "message"])
        if is_new:
            w.writeheader()
        w.writerow(row)

def already_captured(email: str) -> bool:
    acct_dir = COOKIES_ROOT / sanitize_filename(email)
    out_path = acct_dir / f"{sanitize_filename(email)}.txt"
    if not out_path.exists():
        return False
    try:
        size_ok = out_path.stat().st_size >= MIN_VALID_BYTES
        text = out_path.read_text(encoding="utf-8", errors="ignore")
        has_blocks = ("HEADERS =" in text) and ("COOKIES =" in text)
        return size_ok and has_blocks
    except Exception:
        return False

def lower_keys(d: Dict[str, str]) -> Dict[str, str]:
    return {str(k).lower(): str(v) for k, v in d.items()}

def cookies_for_domain(session: requests.Session, domain_suffix: str = ".globalcitizen.org") -> Dict[str, str]:
    out: Dict[str, str] = {}
    for c in session.cookies:
        if c.domain.endswith(domain_suffix):
            out[str(c.name)] = str(c.value)
    return out

def save_account_artifacts(email: str,
                           headers_used: Dict[str, str],
                           session: requests.Session,
                           logger: logging.Logger):
    acct_dir = COOKIES_ROOT / sanitize_filename(email)
    acct_dir.mkdir(parents=True, exist_ok=True)
    out_path = acct_dir / f"{sanitize_filename(email)}.txt"

    h = lower_keys(dict(headers_used))
    h.pop("cookie", None)

    ck = cookies_for_domain(session, ".globalcitizen.org")

    def dict_literal(d: Dict[str, str]) -> str:
        items = [f'    "{k}": "{d[k]}"' for k in sorted(d.keys())]
        return "{\n" + ",\n".join(items) + "\n}"

    content = []
    content.append("HEADERS = " + dict_literal(h))
    content.append("")
    content.append("COOKIES = " + dict_literal(ck))
    out_path.write_text("\n".join(content), encoding="utf-8")

    safe_log(logger, "info", f"  ✓ Saved → accountCookies/{sanitize_filename(email)}/{sanitize_filename(email)}.txt")

PROXY_SCHEMES = ("http://", "https://", "socks5://", "socks4://")

def _normalize_proxy_line(raw: str) -> Optional[str]:
    s = raw.strip()
    if not s:
        return None

    scheme = "http://"
    for p in PROXY_SCHEMES:
        if s.startswith(p):
            scheme = p
            s = s[len(p):]
            break

    if "@" in s:
        return f"{scheme}{s}"

    parts = s.split(":")
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        passwd = ":".join(parts[3:])
        if host and port and user and passwd:
            return f"{scheme}{user}:{passwd}@{host}:{port}"

    if len(parts) == 2 and parts[0] and parts[1].isdigit():
        host, port = parts
        return f"{scheme}{host}:{port}"

    return None

def load_proxies(path: Path) -> List[str]:
    if not path.exists():
        return []
    raw_lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(raw_lines) == 1 and raw_lines[0].lower() == "none":
        return []
    normalized: List[str] = []
    for ln in raw_lines:
        norm = _normalize_proxy_line(ln)
        if norm:
            normalized.append(norm)
    print(f"[proxy] {len(normalized)} usable proxies loaded.")
    return normalized

def pick_proxy(proxies: List[str], index_hint: int) -> Optional[Dict[str, str]]:
    if not proxies:
        return None
    proxy = proxies[index_hint % len(proxies)]
    return {"http": proxy, "https": proxy}

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"

def seed_cookies(session: requests.Session, proxies: Optional[Dict[str, str]]):
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": SITE_ORIGIN + "/",
        "DNT": "1",
    }
    session.get(BASE_URL, headers=headers, proxies=proxies, timeout=REQUEST_TIMEOUT)

def get_csrf_from_jar(session: requests.Session) -> Optional[str]:
    jar = session.cookies.get_dict(domain=".globalcitizen.org")
    return jar.get("prod_csrftoken") or jar.get("csrftoken")

def login(session: requests.Session, email: str, password: str, proxies: Optional[Dict[str, str]]) -> Tuple[bool, str]:
    csrf = get_csrf_from_jar(session)
    headers = {
        "Host": "api.globalcitizen.org",
        "Connection": "keep-alive",
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
        "sec-ch-ua-mobile": "?0",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "DNT": "1",
        "apikey": "7292e0560c6b44258c66fd60f0a19a32",
        "Origin": SITE_ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": SITE_ORIGIN + "/",
        "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
        "Content-Type": "application/json; charset=UTF-8",
    }
    if csrf:
        headers["X-CSRFToken"] = csrf

    payload = {
        "username": email,
        "password": password,
        "remember_me": "on",
    }

    try:
        resp = session.post(LOGIN_URL, headers=headers, data=json.dumps(payload),
                            proxies=proxies, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        return False, f"login request error: {e}"

    if resp.status_code >= 200 and resp.status_code < 300:
        try:
            j = resp.json()
            if isinstance(j, dict) and j.get("detail") and "Unable to log in" in str(j.get("detail")):
                return False, str(j.get("detail"))
        except Exception:
            pass
        return True, "login ok"

    msg = f"login http {resp.status_code}"
    try:
        j = resp.json()
        if isinstance(j, dict) and j.get("detail"):
            msg = f"{msg} - {j.get('detail')}"
    except Exception:
        if resp.text:
            msg = f"{msg} - {resp.text[:200]}"

    return False, msg

def fetch_profile(session: requests.Session, proxies: Optional[Dict[str, str]]) -> Tuple[bool, str, Dict[str, str]]:
    csrf = get_csrf_from_jar(session)
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
        "Connection": "keep-alive",
        "DNT": "1",
        "Referer": f"{SITE_ORIGIN}/en/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": DEFAULT_UA,
        "X-Requested-With": "XMLHttpRequest",
        "sec-ch-ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    if csrf:
        headers["X-CSRFToken"] = csrf

    try:
        resp = session.get(PROFILE_URL, headers=headers, proxies=proxies, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        return False, f"profile request error: {e}", headers

    if 200 <= resp.status_code < 300:
        return True, "ok", headers

    msg = f"profile http {resp.status_code}"
    try:
        j = resp.json()
        if isinstance(j, dict) and j.get("detail"):
            msg = f"{msg} - {j.get('detail')}"
    except Exception:
        if resp.text:
            msg = f"{msg} - {resp.text[:200]}"
    return False, msg, headers

def load_accounts(csv_path: Path) -> List[Tuple[str, str]]:
    accounts: List[Tuple[str, str]] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        sniff = f.read(4096)
        f.seek(0)
        has_header = csv.Sniffer().has_header(sniff)
        if has_header:
            r = csv.DictReader(f)
            norm = {k.lower(): k for k in (r.fieldnames or [])}
            ukey = norm.get("email") or norm.get("username") or next(iter(norm), None)
            pkey = norm.get("password") or ("password" if "password" in norm else None)
            if not (ukey and pkey):
                raise ValueError("CSV header must include email/username and password.")
            for row in r:
                e = (row.get(ukey) or "").strip()
                p = (row.get(pkey) or "").strip()
                if e and p:
                    accounts.append((e, p))
        else:
            r = csv.reader(f)
            for row in r:
                if not row or len(row) < 2:
                    continue
                e = (row[0] or "").strip()
                p = (row[1] or "").strip()
                if e and p:
                    accounts.append((e, p))
    return accounts

def process_account(idx: int, email: str, password: str, proxies_list: List[str],
                    max_retries: int, force: bool, logger: logging.Logger) -> bool:
    progress = f"{idx+1}/{TOTAL_ACCOUNTS}" if TOTAL_ACCOUNTS else f"{idx+1}"

    if not force and already_captured(email):
        safe_log(logger, "info", f"[{progress}] {email} — Skipping (already captured)")
        return True

    safe_log(logger, "info", f"[{progress}] {email} — Starting")

    attempt = 0
    success = False
    last_err = None

    while attempt < max_retries and not success:
        attempt += 1
        pxy = pick_proxy(proxies_list, index_hint=idx + attempt - 1)
        session = requests.Session()
        if pxy:
            session.proxies = pxy

        try:
            seed_cookies(session, pxy)

            ok, msg = login(session, email, password, pxy)
            if not ok:
                last_err = f"login failed: {msg}"
                safe_log(logger, "info", f"  ⚠️  Attempt {attempt}/{max_retries}: {last_err}")
                append_error_row({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "email": email, "attempt": str(attempt), "message": last_err
                })
                time.sleep(0.5)
                continue

            safe_log(logger, "info", "  • Logged in — fetching profile …")
            ok2, msg2, profile_headers_used = fetch_profile(session, pxy)
            if not ok2:
                last_err = f"profile failed: {msg2}"
                safe_log(logger, "info", f"  ⚠️  Attempt {attempt}/{max_retries}: {last_err}")
                append_error_row({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "email": email, "attempt": str(attempt), "message": last_err
                })
                time.sleep(0.5)
                continue

            save_account_artifacts(email, profile_headers_used, session, logger)
            success = True
            safe_log(logger, "info", f"[{progress}] {email} — ✅ captured")

        except requests.RequestException as e:
            last_err = f"network error: {e}"
            safe_log(logger, "info", f"  ⚠️  Attempt {attempt}/{max_retries}: {last_err}")
            append_error_row({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "email": email, "attempt": str(attempt), "message": last_err
            })
            time.sleep(0.5)
        except Exception as e:
            last_err = f"unhandled error: {e}"
            safe_log(logger, "info", f"  ⚠️  Attempt {attempt}/{max_retries}: {last_err}")
            append_error_row({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "email": email, "attempt": str(attempt), "message": last_err
            })
            time.sleep(0.5)

    if not success:
        safe_log(logger, "error", f"[{progress}] {email} — ✗ Failed after {max_retries} attempts: {last_err or 'No details'}")

    return success

def run(verbose: bool, max_retries: int, force: bool, workers: int):
    logger = setup_logging(verbose)
    safe_log(logger, "info", "cookieCapture (requests) starting")
    COOKIES_ROOT.mkdir(parents=True, exist_ok=True)

    proxies = load_proxies(PROXIES_TXT)
    if proxies:
        safe_log(logger, "info", f"Loaded {len(proxies)} usable proxies.")
    else:
        safe_log(logger, "info", "No proxies in use (either 'none' or none valid).")

    if not ACCOUNTS_CSV.exists():
        safe_log(logger, "error", f"Missing {ACCOUNTS_CSV.name}")
        sys.exit(1)
    accounts = load_accounts(ACCOUNTS_CSV)
    set_total_accounts(len(accounts))
    safe_log(logger, "info", f"Loaded {len(accounts)} accounts from {ACCOUNTS_CSV.name}")

    successes = 0
    failures = 0

    if workers <= 1:
        for i, (email, password) in enumerate(accounts):
            ok = process_account(i, email, password, proxies, max_retries, force, logger)
            successes += int(ok)
            failures += int(not ok)
    else:
        if workers > 20:
            safe_log(logger, "warning", f"--workers {workers} is high; requests is light but remote rate-limits may kick in.")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            for i, (email, password) in enumerate(accounts):
                futures.append(pool.submit(process_account, i, email, password, proxies, max_retries, force, logger))
            for fut in as_completed(futures):
                try:
                    ok = fut.result()
                    successes += int(ok)
                    failures += int(not ok)
                except Exception as e:
                    failures += 1
                    safe_log(logger, "error", f"Worker failed: {e}")

    total = len(accounts)
    safe_log(logger, "info", "\n========== Summary ==========")
    safe_log(logger, "info", f"Total accounts: {total} | Successes: {successes} | Failures: {failures}")
    safe_log(logger, "info", "\nAll done ✅")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Capture headers+cookies via Requests after API login.")
    ap.add_argument("--verbose", action="store_true", help="Verbose console output")
    ap.add_argument("--retries", type=int, default=DEFAULT_MAX_RETRIES, help="Retries per account (default 3)")
    ap.add_argument("--force", action="store_true", help="Overwrite existing captures (do not skip)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Parallel workers (default {DEFAULT_WORKERS})")
    args = ap.parse_args()

    run(verbose=args.verbose, max_retries=args.retries, force=args.force, workers=args.workers)
