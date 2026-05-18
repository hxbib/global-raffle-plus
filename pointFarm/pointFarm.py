
from __future__ import annotations

import re
import json
import time
import signal
import random
import logging
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Iterable
from logging.handlers import RotatingFileHandler
from ast import literal_eval
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


DEFAULT_BASE_URL = "https://www.globalcitizen.org/api/actions/{id}/complete/"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_IDS_FILE = SCRIPT_DIR / "ids.txt"
DEFAULT_ACCOUNTS_DIR = SCRIPT_DIR / "accountCookies"
DEFAULT_PROXIES_FILE = SCRIPT_DIR / "proxies.txt"
LOGS_DIR = SCRIPT_DIR / "logs"
RESP_DIR = SCRIPT_DIR / "responses"
STATE_DIR = SCRIPT_DIR / "state"

DEFAULT_TIMEOUT = 30
DEFAULT_SLEEP = 0.5
DEFAULT_MAX_RETRIES = 8
DEFAULT_BACKOFF_MIN = 1.0
DEFAULT_BACKOFF_MAX = 16.0
DEFAULT_WORKERS = 100

DATA_PAYLOAD = {
    "follow_partner": None,
    "action_completion_url": "https://www.globalcitizen.org/en/action/join-the-gstf/"
}


def setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("runner")
    logger.setLevel(logging.INFO)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    ch.setFormatter(ch_fmt)
    logger.addHandler(ch)

    fh = RotatingFileHandler(
        LOGS_DIR / "run.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.INFO)
    fh_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    fh.setFormatter(fh_fmt)
    logger.addHandler(fh)

    return logger

log = setup_logging()


class GracefulExit:
    stop = False

def _signal_handler(signum, frame):
    log.warning("Received signal %s — finishing current task and stopping...", signum)
    GracefulExit.stop = True

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _find_dict_block(text: str, label: str) -> str:
    m = re.search(rf"{label}\s*=", text, flags=re.IGNORECASE)
    if not m:
        raise ValueError(f"{label} block not found")
    i = m.end()

    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] != "{":
        i = text.find("{", i)
        if i == -1:
            raise ValueError(f"{label} block opening '{{' not found")

    start = i
    depth = 0
    in_s = False
    in_d = False
    esc = False
    i -= 1
    while i + 1 < len(text):
        i += 1
        ch = text[i]

        if esc:
            esc = False
            continue

        if ch == "\\":
            if in_s or in_d:
                esc = True
            continue

        if ch == "'" and not in_d:
            in_s = not in_s
            continue

        if ch == '"' and not in_s:
            in_d = not in_d
            continue

        if in_s or in_d:
            continue

        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i+1]

    raise ValueError(f"{label} block appears unbalanced (no closing '}}').")


def _fix_double_double_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 4 and s.startswith('""') and s.endswith('""'):
        return s[1:-1]
    if s.startswith('""') and s.endswith('"'):
        s = s[1:]
    if s.startswith('"') and s.endswith('""'):
        s = s[:-1]
    return s

def _strip_wrapping_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s

def _normalize_dict_text_for_eval(text: str) -> str:
    t = text.strip().replace("\ufeff", "")
    t = re.sub(r'"""', r'""', t)
    return t

def _parse_kv_loose(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    m = re.search(r'^\s*\{(.*)\}\s*$', text, flags=re.DOTALL)
    body = m.group(1) if m else text

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line == ",":
            continue

        kv = re.match(r'^\s*"([^"]+)"\s*:\s*(.+?)(,?\s*)$', line)
        if not kv:
            kv = re.match(r"^\s*'([^']+)'\s*:\s*(.+?)(,?\s*)$", line)
        if not kv:
            continue

        key = kv.group(1)
        val_token = kv.group(2).strip()

        val_token = _fix_double_double_quotes(val_token)
        val = _strip_wrapping_quotes(val_token).strip()

        out[str(key)] = str(val)

    if not out:
        raise ValueError("Loose parser could not find any key/value pairs.")
    return out

def _try_literal_eval_dict(text: str) -> dict:
    try:
        return literal_eval(text)
    except Exception:
        pass

    fixed = _normalize_dict_text_for_eval(text)
    try:
        return literal_eval(fixed)
    except Exception:
        pass

    try:
        json_like = fixed
        json_like = re.sub(r"'", '"', json_like)
        json_like = re.sub(r",\s*([}\]])", r"\1", json_like)
        return json.loads(json_like)
    except Exception:
        return _parse_kv_loose(text)

def parse_headers_cookies(text: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    headers_str = _find_dict_block(text, "HEADERS")
    cookies_str = _find_dict_block(text, "COOKIES")

    headers = _try_literal_eval_dict(headers_str)
    cookies = _try_literal_eval_dict(cookies_str)

    if not isinstance(headers, dict) or not isinstance(cookies, dict):
        raise ValueError("Parsed HEADERS/COOKIES are not dicts.")

    headers = {str(k): str(v) for k, v in headers.items()}
    cookies = {str(k): str(v) for k, v in cookies.items()}
    return headers, cookies

def load_account_files(accounts_dir: Path) -> Dict[str, Path]:
    accounts: Dict[str, Path] = {}
    if not accounts_dir.exists():
        raise FileNotFoundError(f"Accounts directory not found: {accounts_dir}")

    for sub in sorted(accounts_dir.iterdir()):
        if not sub.is_dir():
            continue
        preferred = sub / f"{sub.name}.txt"
        if preferred.exists():
            accounts[sub.name] = preferred
            continue
        txts = list(sub.glob("*.txt"))
        if txts:
            accounts[sub.name] = txts[0]
    return accounts

def load_ids(ids_file: Path) -> List[str]:
    if not ids_file.exists():
        raise FileNotFoundError(f"IDs file not found: {ids_file}")
    with open(ids_file, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


class ProxyPool:
    def __init__(self, proxies_file: Optional[Path] = None, default_scheme: str = "http", entries: Optional[List[Optional[str]]] = None):
        self.default_scheme = default_scheme
        self.entries: List[Optional[str]] = []
        self.invalid_count = 0

        if entries is not None:
            self.entries = list(entries)
        else:
            self._load_from_file(proxies_file or DEFAULT_PROXIES_FILE)

        self.idx = 0
        if self.entries:
            random.shuffle(self.entries)

    def _canonicalize(self, line: str) -> Optional[str]:
        s = line.strip()
        if not s:
            return None
        lo = s.lower()
        if lo in ("none", "direct"):
            return "DIRECT_SENTINEL"

        if "://" in s:
            parsed = urlparse(s)
            if parsed.scheme and parsed.netloc:
                return s

        parts = s.split(":")

        if len(parts) == 4:
            host, port, user, pwd = (p.strip() for p in parts)
            if host and port and user:
                return f"{self.default_scheme}://{user}:{pwd}@{host}:{port}"

        if "@" in s and ":" in s:
            if "://" not in s:
                return f"{self.default_scheme}://{s}"
            return s

        if len(parts) == 2 and parts[0] and parts[1].isdigit():
            host, port = parts
            return f"{self.default_scheme}://{host}:{port}"

        return f"{self.default_scheme}://{s}" if "://" not in s else s

    def _load_from_file(self, proxies_file: Path):
        raw_items: List[str] = []
        if proxies_file.exists():
            with open(proxies_file, "r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if raw:
                        raw_items.append(raw)

        if not raw_items:
            self.entries = [None]
            return

        canonical: List[str] = []
        has_real_proxy = False
        for raw in raw_items:
            url = self._canonicalize(raw)
            if url == "DIRECT_SENTINEL":
                canonical.append(url)
            elif url is None:
                continue
            else:
                p = urlparse(url)
                if not p.scheme or not p.netloc:
                    self.invalid_count += 1
                else:
                    has_real_proxy = True
                    canonical.append(url)

        if has_real_proxy:
            self.entries = [u for u in canonical if u != "DIRECT_SENTINEL"]
        else:
            self.entries = [None]

        if not self.entries:
            self.entries = [None]

        if self.invalid_count:
            log.warning("Skipped %d invalid proxy lines while loading %s",
                        self.invalid_count, proxies_file)

    def next(self) -> Optional[str]:
        p = self.entries[self.idx]
        self.idx = (self.idx + 1) % len(self.entries)
        return p

    def to_requests(self, val: Optional[str]) -> Optional[Dict[str, str]]:
        if val is None:
            return None
        return {"http": val, "https": val}

    def clone(self) -> "ProxyPool":
        return ProxyPool(entries=self.entries, default_scheme=self.default_scheme)

    def unique_proxy_keys(self) -> List[str]:
        keys = []
        for p in self.entries:
            keys.append(p or "DIRECT")
        seen = set()
        out = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out


def load_completed_set(account_key: str) -> set:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{account_key}_completed.json"
    if path.exists():
        try:
            return set(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            log.warning("Failed to load %s, starting with empty set.", path)
    return set()

def save_completed_set(account_key: str, s: set) -> None:
    path = STATE_DIR / f"{account_key}_completed.json"
    path.write_text(json.dumps(sorted(s)), encoding="utf-8")

def append_failed_record(rec: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "failed.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def make_post(
    url: str,
    headers: Dict[str, str],
    cookies: Dict[str, str],
    json_payload: dict,
    proxy_dict: Optional[Dict[str, str]],
    timeout: int,
) -> requests.Response:
    return requests.post(
        url,
        headers=headers,
        cookies=cookies,
        json=json_payload,
        timeout=timeout,
        proxies=proxy_dict,
        allow_redirects=True,
    )

def send_with_retries(
    base_url_tmpl: str,
    action_id: str,
    headers: Dict[str, str],
    cookies: Dict[str, str],
    data_payload: dict,
    proxies: ProxyPool,
    timeout: int,
    max_retries: int,
    backoff_min: float,
    backoff_max: float,
    account_key: str,
) -> Tuple[bool, dict]:
    url = base_url_tmpl.format(id=action_id)
    attempt = 0
    last_err = None

    attempts_404 = 0
    attempts_400 = 0

    tried_proxy_keys_for_429 = set()
    unique_keys = proxies.unique_proxy_keys()
    unique_count = len(unique_keys)

    while True:
        if GracefulExit.stop:
            return False, {
                "email": account_key,
                "id": action_id,
                "status": None,
                "ok": False,
                "proxy": "STOPPED",
                "url": url,
                "error": "Interrupted by user",
                "attempt": attempt,
                "ts": time.time(),
            }

        attempt += 1
        proxy_str = proxies.next()
        proxy_dict = proxies.to_requests(proxy_str)
        proxy_key = proxy_str or "DIRECT"

        try:
            resp = make_post(url, headers, cookies, data_payload, proxy_dict, timeout)
            try:
                body = resp.json()
            except ValueError:
                body = resp.text

            status = resp.status_code
            ok = (200 <= status < 300) or (status == 304)

            result = {
                "email": account_key,
                "id": action_id,
                "status": status,
                "ok": ok,
                "proxy": proxy_key,
                "url": url,
                "response": body,
                "attempt": attempt,
                "ts": time.time(),
            }

            if ok:
                return True, result

            if status == 404:
                attempts_404 += 1
                if attempts_404 > 1:
                    return False, result
                else:
                    last_err = "HTTP 404"
                    log.warning("[%-24s] ID %s attempt %d -> 404; retrying once...",
                                account_key, action_id, attempt)

            elif status == 400:
                attempts_400 += 1
                if attempts_400 >= 3:
                    return False, result
                else:
                    last_err = "HTTP 400"
                    log.warning("[%-24s] ID %s attempt %d -> 400; retrying (cap=3 total)...",
                                account_key, action_id, attempt)

            elif status == 429:
                tried_proxy_keys_for_429.add(proxy_key)
                if len(tried_proxy_keys_for_429) >= unique_count:
                    return False, result
                last_err = "HTTP 429"
                log.warning("[%-24s] ID %s attempt %d -> 429; rotating proxy (%d/%d tried)...",
                            account_key, action_id, attempt,
                            len(tried_proxy_keys_for_429), unique_count)

            else:
                last_err = f"HTTP {status}"
                log.warning("[%-24s] ID %s attempt %d -> %s; rotating proxy and retrying...",
                            account_key, action_id, attempt, last_err)

        except requests.RequestException as e:
            last_err = repr(e)
            log.warning("[%-24s] ID %s attempt %d -> network error: %s; rotating proxy...",
                        account_key, action_id, attempt, last_err)

        if GracefulExit.stop:
            return False, {
                "email": account_key,
                "id": action_id,
                "status": None,
                "ok": False,
                "proxy": proxy_key,
                "url": url,
                "error": "Interrupted by user",
                "attempt": attempt,
                "ts": time.time(),
            }

        if max_retries > 0 and attempt >= max_retries:
            result = {
                "email": account_key,
                "id": action_id,
                "status": None,
                "ok": False,
                "proxy": proxy_key,
                "url": url,
                "error": last_err,
                "attempt": attempt,
                "ts": time.time(),
            }
            return False, result

        sleep_for = min(backoff_max, backoff_min * (2 ** (attempt - 1)))
        sleep_for = sleep_for * (0.75 + 0.5 * random.random())
        time.sleep(sleep_for)


_INVISIBLE = "".join(chr(c) for c in (0x200B, 0x200C, 0x200D, 0xFEFF))
_INV_RE = re.compile(f"[{re.escape(_INVISIBLE)}]")

def _clean_key_for_match(s: str) -> str:
    if s is None:
        return ""
    s = _INV_RE.sub("", str(s))
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]", "", s)
    return s

def _find_csrf_cookie(cookies: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    cleaned_map = {}
    for k, v in cookies.items():
        cleaned_map[_clean_key_for_match(k)] = (k, v)

    exact = cleaned_map.get("prodcsrftoken")
    if exact and exact[1]:
        return exact

    for ck, (orig_k, v) in cleaned_map.items():
        if v and ("csrftoken" in ck or "csrfmiddlewaretoken" in ck):
            return orig_k, v

    return None, None

def _ensure_csrf_header(headers: Dict[str, str], cookies: Dict[str, str], account_key: str) -> None:
    key, token = _find_csrf_cookie(cookies)
    if token:
        headers["x-csrftoken"] = str(token)
        masked = f"{len(str(token))} chars"
        log.info("[%-24s] Applied x-csrftoken from cookie key '%s' (%s).", account_key, key, masked)
    else:
        keys = list(cookies.keys())
        preview = ", ".join(keys[:20]) + (" ..." if len(keys) > 20 else "")
        log.warning("[%-24s] No csrftoken-like cookie found; x-csrftoken not set. Seen keys: %s",
                    account_key, preview if preview else "(none)")


def process_account(
    account_key: str,
    file_path: Path,
    ids: List[str],
    base_url: str,
    proxies_master: ProxyPool,
    timeout: int,
    max_retries: int,
    backoff_min: float,
    backoff_max: float,
    inter_request_sleep: float,
) -> None:
    RESP_DIR.mkdir(parents=True, exist_ok=True)
    resp_path = RESP_DIR / f"{account_key}.ndjson"

    proxies = proxies_master.clone()

    txt = file_path.read_text(encoding="utf-8", errors="ignore")
    headers, cookies = parse_headers_cookies(txt)

    _ensure_csrf_header(headers, cookies, account_key)

    completed = load_completed_set(account_key)

    total = len(ids)
    remaining_ids = [i for i in ids if i not in completed]
    log.info("[%-24s] Loaded %d IDs (%d remaining, %d already completed)",
             account_key, total, len(remaining_ids), len(completed))

    with open(resp_path, "a", encoding="utf-8") as out:
        for idx, action_id in enumerate(remaining_ids, start=1):
            if GracefulExit.stop:
                log.warning("[%-24s] Stop requested; saving state and exiting account loop.", account_key)
                break

            ok, result = send_with_retries(
                base_url_tmpl=base_url,
                action_id=action_id,
                headers=headers,
                cookies=cookies,
                data_payload=DATA_PAYLOAD,
                proxies=proxies,
                timeout=timeout,
                max_retries=max_retries,
                backoff_min=backoff_min,
                backoff_max=backoff_max,
                account_key=account_key,
            )

            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()

            if ok:
                completed.add(action_id)
                log.info("[%-24s] (%d/%d) ID %s -> %s via %s",
                         account_key, idx, len(remaining_ids), action_id, result.get("status"),
                         result.get("proxy"))
            else:
                log.error("[%-24s] (%d/%d) ID %s FAILED after %d attempts; last=%s",
                          account_key, idx, len(remaining_ids), action_id,
                          result.get("attempt"), result.get("error"))
                append_failed_record(result)

            save_completed_set(account_key, completed)

            if inter_request_sleep > 0 and not GracefulExit.stop:
                time.sleep(inter_request_sleep)

def main():
    parser = argparse.ArgumentParser(description="Multi-Account Action Sender")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="URL template with {id}")
    parser.add_argument("--ids", type=Path, default=DEFAULT_IDS_FILE)
    parser.add_argument("--accounts-dir", type=Path, default=DEFAULT_ACCOUNTS_DIR)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                        help="Sleep between requests (seconds)")
    parser.add_argument("--proxies", type=Path, default=DEFAULT_PROXIES_FILE)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES,
                        help="0 = infinite retries (except 404/400/429 special rules)")
    parser.add_argument("--backoff", type=float, nargs=2,
                        default=[DEFAULT_BACKOFF_MIN, DEFAULT_BACKOFF_MAX],
                        metavar=("MIN", "MAX"),
                        help="Exponential backoff bounds (seconds)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="Number of concurrent accounts to process")
    args = parser.parse_args()

    base_url = args.base_url
    if "{id}" not in base_url:
        raise ValueError("--base-url must contain '{id}' placeholder")

    ids = load_ids(args.ids)
    log.info("Loaded %d IDs from %s", len(ids), args.ids)

    accounts = load_account_files(args.accounts_dir)
    if not accounts:
        log.error("No account files found in %s", args.accounts_dir)
        return

    log.info("Discovered %d accounts in %s", len(accounts), args.accounts_dir)

    proxies_master = ProxyPool(args.proxies)
    direct_only = proxies_master.entries == [None]
    if direct_only:
        log.info("Proxy mode: DIRECT only (no real proxies listed).")
    else:
        log.info("Proxy mode: %d proxies loaded (DIRECT disabled).", len(proxies_master.entries))

    futures = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        for account_key, file_path in accounts.items():
            if GracefulExit.stop:
                log.warning("Stop requested before starting next account; exiting.")
                break
            log.info("[%-24s] Starting", account_key)
            futures.append(
                executor.submit(
                    process_account,
                    account_key,
                    file_path,
                    ids,
                    base_url,
                    proxies_master,
                    args.timeout,
                    args.max_retries,
                    args.backoff[0],
                    args.backoff[1],
                    args.sleep,
                )
            )

        try:
            for _ in as_completed(futures):
                if GracefulExit.stop:
                    break
        except KeyboardInterrupt:
            log.warning("Keyboard interrupt received; requesting graceful stop...")
            GracefulExit.stop = True

    log.info("All done.")

if __name__ == "__main__":
    main()
