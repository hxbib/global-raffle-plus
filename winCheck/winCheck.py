
from __future__ import annotations

import re
import sys
import json
import time
import random
import signal
import logging
import argparse
import threading
from ast import literal_eval
from pathlib import Path
from typing import Dict, Tuple, Optional, Any, List
from logging.handlers import RotatingFileHandler

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
LOGS_DIR = SCRIPT_DIR / "logs"
RESP_DIR = SCRIPT_DIR / "responses"

DEFAULT_ACCOUNTS_DIR = SCRIPT_DIR / "accountCookies"
DEFAULT_PROXIES_FILE = SCRIPT_DIR / "proxies.txt"

DEFAULT_TIMEOUT = 30
DEFAULT_SLEEP = 0.2
DEFAULT_BACKOFF_MIN = 1.0
DEFAULT_BACKOFF_MAX = 20.0
DEFAULT_WORKERS = 4

GRAPHQL_URL = "https://www.globalcitizen.org/en/api/graph/graphql/"
DEFAULT_APIKEY = "7292e0560c6b44258c66fd60f0a19a32"


def setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("rewards")
    logger.setLevel(logging.INFO)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
    logger.addHandler(ch)

    fh = RotatingFileHandler(LOGS_DIR / "run.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))
    logger.addHandler(fh)

    return logger

log = setup_logging()


class StopFlag:
    stop = False

def _handle_sig(signum, frame):
    log.warning("Received signal %s — finishing current task(s) and stopping...", signum)
    StopFlag.stop = True

signal.signal(signal.SIGINT, _handle_sig)
signal.signal(signal.SIGTERM, _handle_sig)


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
            raise ValueError(f"{label} opening '{{' not found")
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
        if ch == "\\" and (in_s or in_d):
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
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    raise ValueError(f"{label} dict not closed '}}'")

def _normalize(text: str) -> str:
    t = text.strip().replace("\ufeff", "")
    t = re.sub(r'"""', r'""', t)
    return t

def _loose_parse(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    body = text.strip()
    if body.startswith("{") and body.endswith("}"):
        body = body[1:-1]
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line == ",":
            continue
        m = re.match(r'^\s*("([^"]+)"|\'([^\']+)\')\s*:\s*(.+?)(,?\s*)$', line)
        if not m:
            continue
        key = m.group(2) or m.group(3)
        val_token = m.group(4).strip()
        if val_token.startswith('""') and val_token.endswith('""'):
            val_token = val_token[1:-1]
        if (val_token[0] == val_token[-1]) and val_token[0] in ('"', "'"):
            val = val_token[1:-1]
        else:
            val = val_token
        out[str(key)] = str(val)
    if not out:
        raise ValueError("Could not parse dict loosely.")
    return out

def _try_eval_dict(text: str) -> dict:
    try:
        return literal_eval(text)
    except Exception:
        pass
    fixed = _normalize(text)
    try:
        return literal_eval(fixed)
    except Exception:
        pass
    try:
        jlike = re.sub(r",\s*([}\]])", r"\1", re.sub(r"'", '"', fixed))
        return json.loads(jlike)
    except Exception:
        return _loose_parse(text)

def parse_headers_cookies(blob: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    h_str = _find_dict_block(blob, "HEADERS")
    c_str = _find_dict_block(blob, "COOKIES")
    headers = _try_eval_dict(h_str)
    cookies = _try_eval_dict(c_str)
    if not isinstance(headers, dict) or not isinstance(cookies, dict):
        raise ValueError("HEADERS/COOKIES are not dicts")
    headers = {str(k): str(v) for k, v in headers.items()}
    cookies = {str(k): str(v) for k, v in cookies.items()}
    return headers, cookies


def _clean_key(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)
    return re.sub(r"[^a-z0-9]", "", s)

def ensure_csrf_header(headers: Dict[str, str], cookies: Dict[str, str], acct: str) -> None:
    pick = None
    for k, v in cookies.items():
        ck = _clean_key(k)
        if ck == "prodcsrftoken" and v:
            pick = v
            break
    if not pick:
        for k, v in cookies.items():
            ck = _clean_key(k)
            if v and ("csrftoken" in ck or "csrfmiddlewaretoken" in ck):
                pick = v
                break
    if pick:
        headers["x-csrftoken"] = str(pick)
        log.info("[%-24s] Injected x-csrftoken (%d chars).", acct, len(str(pick)))
    else:
        log.warning("[%-24s] No csrftoken-like cookie found; proceeding without x-csrftoken.", acct)


def discover_accounts(root: Path) -> Dict[str, Path]:
    if not root.exists():
        raise FileNotFoundError(f"Accounts dir not found: {root}")
    out: Dict[str, Path] = {}
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        preferred = sub / f"{sub.name}.txt"
        if preferred.exists():
            out[sub.name] = preferred
        else:
            txts = list(sub.glob("*.txt"))
            if txts:
                out[sub.name] = txts[0]
    return out

def load_all_proxies(proxies_file: Path) -> List[str]:
    if not proxies_file.exists():
        return []
    
    proxies = []
    for raw in proxies_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        lo = line.lower()
        if lo in ("none", "direct"):
            continue
        if "://" not in line:
            parts = line.split(":")
            if len(parts) == 4:
                host, port, username, password = parts
                if port.isdigit():
                    line = f"http://{username}:{password}@{host}:{port}"
            elif len(parts) == 2 and parts[1].isdigit():
                line = "http://" + line
            elif "@" in line and ":" in line:
                line = "http://" + line
        proxies.append(line)
    return proxies

def proxy_to_requests(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def gql_body(after_cursor: Optional[str], aggregated_status: str) -> dict:
    return {
        "operationName": "GetEnteredRewards",
        "variables": {
            "first": 20,
            "after": after_cursor,
            "aggregatedStatus": aggregated_status,
            "last": None,
            "before": None,
        },
        "query": (
            "query GetEnteredRewards($first: Int, $after: String, $last: Int, $before: String, "
            "$aggregatedStatus: EnteredRewardAggregatedStatus) {"
            "  viewer {"
            "    id profileUrl "
            "    enteredRewards(first: $first, after: $after, last: $last, before: $before, aggregatedStatus: $aggregatedStatus) {"
            "      edges { node { id internalId count monthlyCount status redemption { id internalId expiresAt acceptUrl __typename } "
            "        reward { id internalId isLive name url date drawingDate drawingEndDate instantWin allowMultipleEnter "
            "          heroImage { thumbnailUrl(size: SIZE_800_450) __typename } "
            "          location { id name postalCode state city country { id name __typename } __typename } __typename } __typename } __typename } "
            "      pageInfo { startCursor endCursor hasNextPage hasPreviousPage __typename } __typename "
            "    } __typename "
            "  } "
            "}"
        ),
    }


def bounded_backoff(attempt: int, bmin: float, bmax: float) -> float:
    base = min(bmax, bmin * (2 ** (attempt - 1)))
    return base * (0.75 + 0.5 * random.random())

def fetch_all_pages(session: requests.Session,
                    headers: Dict[str, str],
                    cookies: Dict[str, str],
                    proxies_list: List[str],
                    timeout: int,
                    backoff_min: float,
                    backoff_max: float,
                    acct: str,
                    aggregated_status: str,
                    resp_sink) -> List[dict]:
    nodes: List[dict] = []
    after = None
    page = 0
    attempt = 0
    current_proxy = None
    status_counts: Dict[int, int] = {}
    
    if proxies_list:
        current_proxy = random.choice(proxies_list)
        log.info("[%-24s] Starting with random proxy: %s", acct, current_proxy)
    else:
        log.info("[%-24s] No proxies available, using direct connection", acct)

    while True:
        if StopFlag.stop:
            break

        body = gql_body(after, aggregated_status)
        attempt += 1
        
        proxy_dict = proxy_to_requests(current_proxy)
        
        try:
            r = session.post(
                GRAPHQL_URL,
                json=body,
                headers=headers,
                cookies=cookies,
                proxies=proxy_dict,
                timeout=timeout,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            log.error("[%-24s] Network error on page %s: %r", acct, page + 1, e)
            time.sleep(bounded_backoff(attempt, backoff_min, backoff_max))
            continue

        status = r.status_code
        status_counts[status] = status_counts.get(status, 0) + 1
        
        text = None
        try:
            data = r.json()
        except ValueError:
            text = r.text
            data = None

        resp_sink.write(json.dumps({
            "page": page + 1,
            "status_code": status,
            "ok": 200 <= status < 300,
            "body": data if data is not None else text,
            "proxy": current_proxy,
            "ts": time.time(),
        }, ensure_ascii=False) + "\n")
        resp_sink.flush()

        if status == 429:
            log.warning("[%-24s] 429 received (page %d) with proxy: %s", acct, page + 1, current_proxy or "DIRECT")
            
            if proxies_list and len(proxies_list) > 1:
                available_proxies = [p for p in proxies_list if p != current_proxy]
                if available_proxies:
                    old_proxy = current_proxy
                    current_proxy = random.choice(available_proxies)
                    log.info("[%-24s] Switching from proxy %s to %s due to 429", acct, old_proxy or "DIRECT", current_proxy)
                    attempt = 1
                    continue
                else:
                    log.warning("[%-24s] No other proxies available, backing off with current proxy", acct)
            else:
                log.warning("[%-24s] No proxy rotation available, backing off", acct)
            
            time.sleep(bounded_backoff(attempt, backoff_min, backoff_max))
            continue

        if not (200 <= status < 300):
            log.error("[%-24s] HTTP %s (page %d) with proxy: %s. Will retry with backoff.", 
                     acct, status, page + 1, current_proxy or "DIRECT")
            time.sleep(bounded_backoff(attempt, backoff_min, backoff_max))
            continue

        attempt = 0

        viewer = (data or {}).get("data", {}).get("viewer", {})
        entered = viewer.get("enteredRewards", {}) or {}
        edges = entered.get("edges", []) or []
        for edge in edges:
            node = (edge or {}).get("node", {}) or {}
            if node:
                nodes.append(node)

        pageinfo = entered.get("pageInfo", {}) or {}
        has_next = bool(pageinfo.get("hasNextPage"))
        after = pageinfo.get("endCursor")
        page += 1

        log.info("[%-24s] Pulled page %d (%d nodes so far). hasNext=%s", acct, page, len(nodes), has_next)

        if not has_next:
            break

    if status_counts:
        log.info("[%-24s] Status codes encountered: %s", acct, 
                dict(sorted(status_counts.items())))

    return nodes


def process_account(acct_key: str,
                    file_path: Path,
                    proxies_list: List[str],
                    timeout: int,
                    backoff_min: float,
                    backoff_max: float,
                    inter_sleep: float,
                    aggregated_status: str,
                    global_totals: Dict[str, int],
                    global_lock: threading.Lock) -> None:
    RESP_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESP_DIR / f"{acct_key}.ndjson", "a", encoding="utf-8") as sink:
        raw = file_path.read_text(encoding="utf-8", errors="ignore")
        headers, cookies = parse_headers_cookies(raw)

        headers.setdefault("content-type", "application/json")
        headers.setdefault("accept", "*/*")
        headers.setdefault("apikey", DEFAULT_APIKEY)

        ensure_csrf_header(headers, cookies, acct_key)

        if proxies_list:
            log.info("[%-24s] Using proxy rotation with %d proxies available", acct_key, len(proxies_list))
        else:
            log.info("[%-24s] Using DIRECT connection (no proxies)", acct_key)

        with requests.Session() as s:
            nodes = fetch_all_pages(
                s, headers, cookies, proxies_list,
                timeout, backoff_min, backoff_max, acct_key, aggregated_status, sink
            )

        per_account: Dict[str, int] = {}
        per_account_rewards: Dict[str, Dict[str, int]] = {}
        
        for n in nodes:
            status = (n.get("status") or "").strip().lower()
            reward_name = ""
            reward_obj = n.get("reward", {})
            if reward_obj and isinstance(reward_obj, dict):
                reward_name = (reward_obj.get("name") or "Unknown Reward").strip()
            
            if status:
                per_account[status] = per_account.get(status, 0) + 1
                
                if reward_name not in per_account_rewards:
                    per_account_rewards[reward_name] = {}
                per_account_rewards[reward_name][status] = per_account_rewards[reward_name].get(status, 0) + 1
                
                with global_lock:
                    global_totals[status] = global_totals.get(status, 0) + 1
                    
                    reward_key = f"REWARD_BREAKDOWN"
                    if reward_key not in global_totals:
                        global_totals[reward_key] = {}
                    if reward_name not in global_totals[reward_key]:
                        global_totals[reward_key][reward_name] = {}
                    global_totals[reward_key][reward_name][status] = global_totals[reward_key][reward_name].get(status, 0) + 1
                
                if status == "winner":
                    print("\n" + "=" * 60)
                    print("\033[1m" + "🚨🚨🚨  W I N   F O U N D  🚨🚨🚨".center(58) + "\033[0m")
                    print(f"🎁 REWARD: {reward_name}")
                    print("=" * 60 + "\n")

        not_a_winner = per_account.get("not_a_winner", 0)
        others = {k: v for k, v in per_account.items() if k != "not_a_winner"}

        log.info("[%-24s] Status tallies: not_a_winner=%d; others=%s",
                 acct_key, not_a_winner, json.dumps(others) if others else "{}")

        print(f"\n--- Account: {acct_key} ---")
        print(f"Total rewards checked: {len(nodes)}")
        print(f"not_a_winner: {not_a_winner}")
        if others:
            print("other reward statuses:")
            for k, v in sorted(others.items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"  - {k}: {v}")
        else:
            print("other reward statuses: none")
        
        if per_account_rewards:
            print("rewards breakdown:")
            for reward_name, statuses in per_account_rewards.items():
                total_for_reward = sum(statuses.values())
                print(f"  📍 {reward_name} ({total_for_reward} total):")
                for status, count in sorted(statuses.items(), key=lambda kv: (-kv[1], kv[0])):
                    print(f"    - {status}: {count}")
        print("---------------------------\n")

        if inter_sleep > 0 and not StopFlag.stop:
            time.sleep(inter_sleep)


def main():
    ap = argparse.ArgumentParser(description="Global Citizen — Rewards Status Checker (safe)")
    ap.add_argument("--accounts-dir", type=Path, default=DEFAULT_ACCOUNTS_DIR)
    ap.add_argument("--proxies", type=Path, default=DEFAULT_PROXIES_FILE,
                    help="Optional; all valid proxy lines used with random rotation on 429 errors.")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                    help="Sleep between accounts (seconds).")
    ap.add_argument("--backoff", type=float, nargs=2,
                    default=[DEFAULT_BACKOFF_MIN, DEFAULT_BACKOFF_MAX],
                    metavar=("MIN", "MAX"),
                    help="Exponential backoff bounds for retries.")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="Concurrent accounts to process.")
    ap.add_argument("--status", type=str, default="EXPIRED",
                    help="aggregatedStatus variable (EXPIRED | ACTIVE | ALL)")
    args = ap.parse_args()

    accounts = discover_accounts(args.accounts_dir)
    if not accounts:
        log.error("No accounts found under %s", args.accounts_dir)
        sys.exit(2)

    proxies_list = load_all_proxies(args.proxies)
    if proxies_list:
        log.info("Proxy mode: rotation enabled with %d proxies loaded.", len(proxies_list))
    else:
        log.info("Proxy mode: DIRECT (no proxies loaded).")

    log.info("Discovered %d accounts.", len(accounts))

    from concurrent.futures import ThreadPoolExecutor, as_completed

    global_totals: Dict[str, int] = {}
    global_lock = threading.Lock()
    futures = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for acct_key, path in accounts.items():
            if StopFlag.stop:
                break
            log.info("[%-24s] Starting", acct_key)
            futures.append(
                ex.submit(
                    process_account,
                    acct_key,
                    path,
                    proxies_list,
                    args.timeout,
                    args.backoff[0],
                    args.backoff[1],
                    args.sleep,
                    args.status,
                    global_totals,
                    global_lock,
                )
            )

        try:
            for future in as_completed(futures):
                if StopFlag.stop:
                    break
                try:
                    future.result()
                except Exception as e:
                    log.error("Account processing failed: %s", e)
        except KeyboardInterrupt:
            log.warning("Keyboard interrupt — requesting graceful stop…")
            StopFlag.stop = True

    print("\n================== GLOBAL SUMMARY ==================")
    
    reward_breakdown = global_totals.pop("REWARD_BREAKDOWN", {})
    
    total_checked = sum(v for k, v in global_totals.items() if isinstance(v, int))
    naw = global_totals.get("not_a_winner", 0)
    print(f"Total reward statuses seen across all accounts: {total_checked}")
    print(f"not_a_winner: {naw}")
    others = {k: v for k, v in global_totals.items() if k != "not_a_winner" and isinstance(v, int)}
    if others:
        print("other reward statuses found:")
        for k, v in sorted(others.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  - {k}: {v}")
    else:
        print("other reward statuses: none")
    
    if reward_breakdown:
        print("\n🎁 BREAKDOWN BY REWARD:")
        for reward_name, statuses in sorted(reward_breakdown.items()):
            total_for_reward = sum(statuses.values())
            print(f"\n📍 {reward_name} ({total_for_reward} entries total):")
            for status, count in sorted(statuses.items(), key=lambda kv: (-kv[1], kv[0])):
                percentage = (count / total_for_reward) * 100 if total_for_reward > 0 else 0
                print(f"    - {status}: {count} ({percentage:.1f}%)")
    
    print("====================================================\n")

if __name__ == "__main__":
    main()
