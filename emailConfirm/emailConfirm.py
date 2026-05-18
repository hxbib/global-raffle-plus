import os
import sys
import time
import logging
import re
import random
import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from threading import Lock

from seleniumwire import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import WebDriverException, TimeoutException
from selenium.webdriver.common.by import By

HEADLESS = True
WORKERS = 20
PAGE_LOAD_TIMEOUT = 60
DOM_READY_TIMEOUT = 45
IDLE_GRACE = 0.6
MAX_RETRIES_PER_LINK = 6

USE_CHROMIUM = False
DEFAULT_CHROMIUM_MAC = "/Applications/Chromium.app/Contents/MacOS/Chromium"
CHROME_BINARY = ""

RESUB_CONFIRM_TIMEOUT = 15

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "linkrunner_logs"
OUTPUT_DIR = ROOT / "linkrunner_output"
PROXY_LIST_PATH = ROOT / "proxies.txt"

for p in (LOG_DIR, OUTPUT_DIR):
    p.mkdir(parents=True, exist_ok=True)

RUN_STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
LOG_PATH = LOG_DIR / f"run-{RUN_STAMP}.log"
SUMMARY_PATH = LOG_DIR / f"summary-{RUN_STAMP}.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_PATH), encoding="utf-8"),
    ],
)
for noisy in [
    "seleniumwire", "seleniumwire.backend", "seleniumwire.server",
    "seleniumwire.thirdparty", "urllib3", "WDM",
    "selenium.webdriver.remote.remote_connection",
]:
    logging.getLogger(noisy).setLevel(logging.ERROR)
os.environ["WDM_LOG_LEVEL"] = "0"

_TS_TXT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.txt$")

def pick_newest_timestamped_txt() -> Path:
    candidates = [p for p in ROOT.iterdir() if p.is_file() and _TS_TXT_RE.match(p.name)]
    if not candidates:
        logging.error("No timestamped .txt files found (YYYY-MM-DD_HH-MM-SS.txt).")
        sys.exit(1)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    logging.info(f"Using newest timestamped file: {candidates[0].name}")
    return candidates[0]

def read_links_from_file(path: Path) -> List[str]:
    links = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not links:
        logging.error(f"No links found in {path.name}.")
        sys.exit(1)
    logging.info(f"Loaded {len(links)} links from {path.name}")
    return links

_PROXY_RE_FULL = re.compile(
    r"^(?:(?P<scheme>https?|socks5)://)?"
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
    host = d["host"]; port = d["port"]
    user = d.get("user") or ""; pw = d.get("pw") or ""
    url_no_auth = f"{scheme}://{host}:{port}"
    url_with_auth = f"{scheme}://{user}:{pw}@{host}:{port}" if user and pw else ""
    return {
        "scheme": scheme, "host": host, "port": port,
        "user": user, "pw": pw,
        "url_no_auth": url_no_auth,
        "url_with_auth": url_with_auth,
        "raw": line,
    }

def load_proxies(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        logging.info(f"{path.name} not found. Running without proxy.")
        return []
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) == 1 and lines[0].lower() == "none":
        logging.info("proxies.txt contains 'none' → running without proxy.")
        return []
    parsed: List[Dict[str, str]] = []
    for ln in lines:
        p = parse_proxy_line(ln)
        if p:
            parsed.append(p)
        else:
            logging.warning(f"Skipping invalid proxy line: {ln!r}")
    if parsed:
        logging.info(f"Loaded {len(parsed)} proxies.")
    else:
        logging.info("No valid proxies parsed. Running without proxy.")
    return parsed

def _install_chromedriver_path() -> str:
    try:
        return ChromeDriverManager(cache_valid_range=1).install()
    except TypeError:
        return ChromeDriverManager().install()

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def build_driver(headless: bool, proxy: Optional[Dict[str, str]], page_load_timeout: int) -> webdriver.Chrome:
    chrome_options = Options()

    if CHROME_BINARY:
        chrome_options.binary_location = CHROME_BINARY
        logging.info(f"Using custom Chrome binary: {CHROME_BINARY}")
    elif USE_CHROMIUM:
        if os.path.exists(DEFAULT_CHROMIUM_MAC):
            chrome_options.binary_location = DEFAULT_CHROMIUM_MAC
            logging.info(f"Using Chromium binary: {DEFAULT_CHROMIUM_MAC}")
        else:
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            if os.path.exists(chrome_path):
                chrome_options.binary_location = chrome_path
                logging.info(f"Chromium not found, falling back to Chrome: {chrome_path}")
            else:
                logging.warning("Neither Chromium nor Chrome found in expected locations")

    chrome_options.add_argument("--incognito")
    chrome_options.add_argument("--remote-debugging-pipe")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-client-side-phishing-detection")
    chrome_options.add_argument("--disable-default-apps")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--metrics-recording-only")
    chrome_options.add_argument("--safebrowsing-disable-auto-update")
    chrome_options.add_argument("--ignore-certificate-errors")
    chrome_options.add_argument("--allow-running-insecure-content")
    chrome_options.add_argument("--window-size=1366,900")
    chrome_options.add_argument(f"--user-agent={_UA}")
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--remote-allow-origins=*")

    if headless:
        chrome_options.add_argument("--headless=new")

    chrome_options.set_capability("acceptInsecureCerts", True)

    wire_opts = {
        "request_storage": {"backend": "memory", "max_entries": 200},
        "disable_encoding": True,
        "verify_ssl": False,
        "capture_headers": False,
        "capture_request_body": False,
        "capture_response_body": False,
    }

    if proxy:
        proxy_url = proxy["url_with_auth"] or proxy["url_no_auth"]
        wire_opts["proxy"] = {
            "http": proxy_url,
            "https": proxy_url,
            "no_proxy": "localhost,127.0.0.1",
        }

    service = Service(_install_chromedriver_path())
    driver = webdriver.Chrome(service=service, options=chrome_options, seleniumwire_options=wire_opts)
    driver.set_page_load_timeout(page_load_timeout)
    return driver

def handle_insecure_warning(driver) -> None:
    try:
        time.sleep(0.4)
        candidates = [
            "#proceed-button",
            "#primary-button",
            "#proceed-link",
            "button#proceed-button",
            "a#proceed-link",
        ]
        for sel in candidates:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    logging.info("Insecure interstitial → continuing")
                    el.click()
                    time.sleep(0.6)
                    return
            except Exception:
                continue

        try:
            for b in driver.find_elements(By.TAG_NAME, "button"):
                txt = (b.text or "").strip().lower()
                if "continue" in txt or "proceed" in txt:
                    logging.info("Insecure interstitial (text) → continuing")
                    b.click()
                    time.sleep(0.6)
                    return
        except Exception:
            pass

        driver.execute_script(
            """
            const ids = ['proceed-button','primary-button','proceed-link'];
            for (const id of ids) {
              const el = document.getElementById(id);
              if (el) { el.click(); return; }
            }
            """
        )
        time.sleep(0.4)
    except Exception:
        pass

UNSUB_TEXT = "you've successfully been unsubscribed from transactional email channel messages for global citizen."
RESUB_OK_TEXT = "you've successfully been resubscribed to transactional messages for global citizen."
ADDR_CONF_TEXT = "your address has been successfully confirmed"

def click_resubscribe_button(driver) -> bool:
    xpaths = [
        "//button[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='resubscribe']",
        "//a[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='resubscribe']",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'resubscribe')]",
        "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'resubscribe')]",
    ]
    for xp in xpaths:
        try:
            el = driver.find_element(By.XPATH, xp)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.1)
            el.click()
            return True
        except Exception:
            continue
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, "button, a"):
            if "resubscribe" in (el.text or "").strip().lower():
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.1)
                el.click()
                return True
    except Exception:
        pass
    try:
        clicked = driver.execute_script(
            """
            const candidates = [
              "button#resubscribe", "a#resubscribe",
              "button[data-action='resubscribe']", "a[data-action='resubscribe']"
            ];
            for (const sel of candidates) {
              const el = document.querySelector(sel);
              if (el) { el.click(); return true; }
            }
            return false;
            """
        )
        if clicked:
            return True
    except Exception:
        pass
    return False

def handle_resubscribe_if_present(driver, confirm_timeout: int = RESUB_CONFIRM_TIMEOUT) -> Tuple[bool, bool]:
    try:
        page = (driver.page_source or "").lower()
        if UNSUB_TEXT in page:
            logging.info("Unsubscribe confirmation detected → attempting resubscribe")
            clicked = click_resubscribe_button(driver)
            if not clicked:
                logging.warning("Could not find a 'Resubscribe' control")
                return True, False
            WebDriverWait(driver, confirm_timeout).until(
                lambda d: RESUB_OK_TEXT in (d.page_source or "").lower()
            )
            logging.info("Resubscribe confirmation detected")
            time.sleep(0.5)
            return True, True
    except Exception as e:
        logging.warning(f"Resubscribe flow did not complete: {e}")
        return True, False
    return False, False

def wait_page_fully_loaded(driver, url: str, dom_timeout: int, idle_grace: float) -> None:
    WebDriverWait(driver, dom_timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(idle_grace)

def save_artifacts(driver, label: str):
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        shot = OUTPUT_DIR / f"{ts}_{label}.png"
        html = OUTPUT_DIR / f"{ts}_{label}.html"
        driver.save_screenshot(str(shot))
        html.write_text(driver.page_source, encoding="utf-8")
        logging.info(f"Saved artifacts: {shot.name}, {html.name}")
    except Exception:
        pass

def sanitize_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)

def same_host(u1: str, u2: str) -> bool:
    try:
        return urlparse(u1).netloc == urlparse(u2).netloc
    except Exception:
        return False

def best_status_code(driver, target_url: str) -> Optional[int]:
    final_url = getattr(driver, "current_url", "") or target_url
    last_status = None
    try:
        for req in driver.requests:
            if getattr(req, "response", None):
                last_status = req.response.status_code
        for req in reversed(driver.requests):
            if getattr(req, "response", None) and same_host(req.url, final_url):
                if 200 <= req.response.status_code < 300:
                    return req.response.status_code
        for req in reversed(driver.requests):
            if getattr(req, "response", None) and same_host(req.url, target_url):
                if 200 <= req.response.status_code < 300:
                    return req.response.status_code
        return last_status
    except Exception:
        return last_status

COUNTS_LOCK = Lock()
COUNTS = {
    "unsub_detected": 0,
    "resub_success": 0,
    "address_confirmed": 0,
}

URL_MAPPING_LOCK = Lock()
URL_TO_INDEX = {}
TOTAL_URLS = 0

def get_url_index(url: str) -> str:
    with URL_MAPPING_LOCK:
        if url not in URL_TO_INDEX:
            URL_TO_INDEX[url] = len(URL_TO_INDEX) + 1
        return f"URL #{URL_TO_INDEX[url]}"

def set_total_urls(count: int):
    global TOTAL_URLS
    TOTAL_URLS = count

def inc_counter(key: str, delta: int = 1):
    with COUNTS_LOCK:
        COUNTS[key] += delta

def open_link_once(
    link: str,
    headless: bool,
    page_load_timeout: int,
    dom_timeout: int,
    idle_grace: float,
    proxy: Optional[Dict[str, str]],
    worker_id: str = "",
    url_progress: str = "",
) -> Tuple[str, bool, Optional[int], str, bool, bool, bool]:
    driver = None
    proxy_desc = (proxy["raw"] if proxy else "NO_PROXY")
    url_index = get_url_index(link)
    worker_prefix = f"[{worker_id}]" if worker_id else ""
    progress_info = f" ({url_progress})" if url_progress else ""
    
    try:
        logging.info(f"{worker_prefix} Launching browser for {url_index}{progress_info} | Proxy: {proxy_desc}")
        driver = build_driver(headless=headless, proxy=proxy, page_load_timeout=page_load_timeout)

        driver.get(link)

        handle_insecure_warning(driver)

        wait_page_fully_loaded(driver, link, dom_timeout=dom_timeout, idle_grace=idle_grace)

        unsub_detected, resub_ok = handle_resubscribe_if_present(driver)

        page_lower = (driver.page_source or "").lower()
        address_confirmed = (ADDR_CONF_TEXT in page_lower)

        status = best_status_code(driver, link)
        if status is None:
            logging.warning(f"{worker_prefix} [FAIL] {url_index}{progress_info} (no status captured)")
            return (link, False, None, "No status captured", unsub_detected, resub_ok, address_confirmed)

        if 200 <= status < 300:
            logging.info(f"{worker_prefix} [OK] {url_index}{progress_info} ({status})")
            return (link, True, status, "ok", unsub_detected, resub_ok, address_confirmed)
        else:
            logging.warning(f"{worker_prefix} [FAIL] {url_index}{progress_info} ({status})")
            save_artifacts(driver, f"status_{status}_{sanitize_filename(link)[:40]}")
            return (link, False, status, f"HTTP {status}", unsub_detected, resub_ok, address_confirmed)

    except (TimeoutException, WebDriverException) as e:
        logging.warning(f"{worker_prefix} [FAIL] {url_index}{progress_info} ({type(e).__name__})")
        if driver:
            save_artifacts(driver, f"error_{sanitize_filename(link)[:40]}")
        return (link, False, None, f"{type(e).__name__}: {e}", False, False, False)

    except Exception as e:
        logging.warning(f"{worker_prefix} [FAIL] {url_index}{progress_info} (Exception)")
        if driver:
            save_artifacts(driver, f"error_{sanitize_filename(link)[:40]}")
        return (link, False, None, f"Exception: {e}", False, False, False)

    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass
        logging.info(f"{worker_prefix} Closed browser for {url_index}{progress_info}")

def pick_proxy_for_attempt(link_idx: int, attempt: int, proxies: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not proxies:
        return None
    return proxies[(link_idx + attempt) % len(proxies)]

def process_link(index: int, link: str, proxies: List[Dict[str, str]], worker_id: str = "") -> Tuple[bool, Optional[int]]:
    progress_info = f"{index + 1}/{TOTAL_URLS}" if TOTAL_URLS > 0 else f"{index + 1}"
    
    for attempt in range(MAX_RETRIES_PER_LINK + 1):
        proxy = pick_proxy_for_attempt(index, attempt, proxies)
        _, ok, status, _, unsub_detected, resub_ok, address_confirmed = open_link_once(
            link=link,
            headless=HEADLESS,
            page_load_timeout=PAGE_LOAD_TIMEOUT,
            dom_timeout=DOM_READY_TIMEOUT,
            idle_grace=IDLE_GRACE,
            proxy=proxy,
            worker_id=worker_id,
            url_progress=progress_info
        )

        if unsub_detected:
            inc_counter("unsub_detected", 1)
            if resub_ok:
                inc_counter("resub_success", 1)
            return ok, status

        if address_confirmed:
            inc_counter("address_confirmed", 1)
            return ok, status

        if ok:
            return True, status

        time.sleep(0.3)

    return False, None

def write_summary(total_links: int, successes: int, failures: int):
    lines = [
        f"Run timestamp: {RUN_STAMP}",
        f"Total links processed: {total_links}",
        f"Successful loads (2xx): {successes}",
        f"Failed loads: {failures}",
        f"Unsubscribe confirmations detected (resubscribe attempted): {COUNTS['unsub_detected']}",
        f"Resubscribe confirmations succeeded: {COUNTS['resub_success']}",
        f"'Your address has been successfully confirmed' detected: {COUNTS['address_confirmed']}",
        "",
        f"Log file: {LOG_PATH}",
    ]
    try:
        SUMMARY_PATH.write_text("\n".join(lines), encoding="utf-8")
        logging.info(f"Summary written to {SUMMARY_PATH}")
    except Exception as e:
        logging.error(f"Failed to write summary file: {e}")

def main():
    logging.info("========== Link Runner Start ==========")
    logging.info(f"Headless: {HEADLESS} | Workers: {WORKERS} | Chromium preferred: {USE_CHROMIUM}")

    input_path = pick_newest_timestamped_txt()
    links = read_links_from_file(input_path)
    proxies = load_proxies(PROXY_LIST_PATH)
    
    set_total_urls(len(links))

    successes = 0
    failures = 0

    if WORKERS <= 1:
        for i, link in enumerate(links):
            ok, _ = process_link(i, link, proxies, worker_id="Worker-1")
            successes += int(ok)
            failures += int(not ok)
    else:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = []
            for i, link in enumerate(links):
                worker_id = f"Worker-{(i % WORKERS) + 1}"
                futures.append(pool.submit(process_link, i, link, proxies, worker_id))
            
            for fut in as_completed(futures):
                try:
                    ok, _ = fut.result()
                    successes += int(ok)
                    failures += int(not ok)
                except Exception:
                    failures += 1

    total = len(links)
    logging.info("========== Summary ==========")
    logging.info(f"Total links: {total} | Successes: {successes} | Failures: {failures}")
    logging.info(f"Unsub pages detected: {COUNTS['unsub_detected']} | Resub successes: {COUNTS['resub_success']}")
    logging.info(f"Address confirmations: {COUNTS['address_confirmed']}")
    write_summary(total, successes, failures)

if __name__ == "__main__":
    main()
