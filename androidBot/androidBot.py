

import csv
import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Tuple, Optional

from appium import webdriver
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


SERVER_URL: str = "http://127.0.0.1:4723"
DEVICE_NAME: str = "androiddevicenamegoeshere"

APP_PACKAGE: str = "lr.globalcitizen.com"
APP_ACTIVITY: str = "org.globalcitizen.app.MainActivity"

CSV_PATH_ABSOLUTE: str = ""

START_FROM_INDEX: int = 1

CLICK_INTERVAL: float = 0.15
STEP_TIMEOUT: int = 22
ENTER_TAPS: int = 130
STEP_PAUSE: float = 1.0
WAIT_AFTER_TAPS: float = 2.0

LOWER_PHASE_TAPS: int = 50
LOWER_PHASE_Y_OFFSET: int = 60

ENTER_BUTTON_COORDS: Tuple[int, int] = (1286, 1251)

SWIPE_LOGOUT_START: Tuple[int, int] = (989, 1233)
SWIPE_LOGOUT_END: Tuple[int, int] = (989, 884)
SWIPE_DURATION_MS: int = 350

EMAIL_BOUNDS_CENTER: Tuple[int, int] = ((752 + 1808) // 2, (1016 + 1112) // 2)
PWD_BOUNDS_CENTER:   Tuple[int, int] = ((752 + 1808) // 2, (1144 + 1240) // 2)


S = {
    "welcome_login": [
        (AppiumBy.ACCESSIBILITY_ID, "Log in"),
        (AppiumBy.XPATH, "//*[@class='android.widget.Button' and @content-desc='Log in']"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("Log in")'),
    ],
    "use_password":  [
        (AppiumBy.ACCESSIBILITY_ID, "Use password"),
        (AppiumBy.XPATH, "//*[@class='android.widget.Button' and @content-desc='Use password']"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("Use password")'),
    ],
    "festival_tab": [
        (AppiumBy.ACCESSIBILITY_ID, "Festival\nTab 1 of 3"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("Festival")'),
        (AppiumBy.XPATH, "//*[@content-desc='Festival\nTab 1 of 3' or contains(@content-desc,'Festival')]"),
    ],
    "menu_icon_top_left": [
        (AppiumBy.ACCESSIBILITY_ID, "Open profile sidebar"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("Open profile sidebar")'),
    ],
    "logout_button": [
        (AppiumBy.ACCESSIBILITY_ID, "Log out"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Log out")'),
    ],
    "home_marker": [
        (AppiumBy.XPATH, "//*[@content-desc and contains(@content-desc,'Tab')]"),
    ],
}
NOTIF_DENY = (AppiumBy.ID, "com.android.permissioncontroller:id/permission_deny_button")
NOTIF_ALLOW = (AppiumBy.ID, "com.android.permissioncontroller:id/permission_allow_button")


def setup_logger() -> logging.Logger:
    script_dir = Path(__file__).resolve().parent
    os.makedirs(script_dir / "logs", exist_ok=True)

    logger = logging.getLogger("gc_avd_auto")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    ch = logging.StreamHandler(sys.stdout); ch.setFormatter(fmt); logger.addHandler(ch)

    fh = RotatingFileHandler(str(script_dir / "logs" / "run.log"), maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt); logger.addHandler(fh)

    eh = RotatingFileHandler(str(script_dir / "logs" / "errors.log"), maxBytes=2_000_000, backupCount=3)
    eh.setFormatter(fmt); eh.setLevel(logging.ERROR); logger.addHandler(eh)

    big = RotatingFileHandler(str(script_dir / "automation.log"), maxBytes=5_000_000, backupCount=5)
    big.setFormatter(fmt); logger.addHandler(big)

    return logger

logger = setup_logger()


def step_pause(): time.sleep(STEP_PAUSE)

def screenshot_artifacts(driver, label: str):
    try:
        script_dir = Path(__file__).resolve().parent
        os.makedirs(script_dir / "artifacts", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        png = str(script_dir / "artifacts" / f"{ts}_{label}.png")
        xml = str(script_dir / "artifacts" / f"{ts}_{label}.xml")
        driver.save_screenshot(png)
        with open(xml, "w", encoding="utf-8") as f:
            f.write(driver.page_source or "")
        logger.info(f"Saved artifacts: {png}, {xml}")
    except Exception as e:
        logger.warning(f"Could not save artifacts: {e}")

def wait_click_any(driver, locators: List[Tuple[str, str]], timeout=STEP_TIMEOUT):
    end = time.time() + timeout
    while time.time() < end:
        for by, val in locators:
            try:
                WebDriverWait(driver, 2).until(EC.element_to_be_clickable((by, val))).click()
                return True
            except Exception:
                try:
                    driver.find_element(by, val).click()
                    return True
                except Exception:
                    continue
        time.sleep(0.15)
    return False

def on_welcome(driver)->bool:
    try:
        for loc in S["welcome_login"]:
            try:
                WebDriverWait(driver, 1.0).until(EC.visibility_of_element_located(loc))
                return True
            except Exception:
                continue
        return False
    except Exception:
        return False

def tap_xy(driver, x, y):
    driver.execute_script("mobile: shell", {"command":"input","args":["tap", str(x), str(y)]})

def tap_ui_xy(driver, x: int, y: int):
    try:
        driver.execute_script("mobile: clickGesture", {"x": int(x), "y": int(y)})
    except Exception:
        tap_xy(driver, x, y)

def swipe_xy(driver, x1,y1,x2,y2,dur_ms):
    driver.execute_script("mobile: shell", {"command":"input","args":["swipe", str(x1),str(y1),str(x2),str(y2),str(dur_ms)]})

def keyboard_shown(driver) -> bool:
    try:
        return bool(driver.execute_script("mobile: isKeyboardShown", {}))
    except Exception:
        return False

def hide_keyboard(driver):
    try:
        driver.execute_script("mobile: hideKeyboard", {}); time.sleep(0.2)
    except Exception:
        try:
            driver.execute_script("mobile: shell", {"command":"input","args":["keyevent","111"]})
        except Exception:
            pass

def _upper(s: str) -> str:
    return (s or "").upper()

def _center_of_rect(rect: dict) -> Tuple[int, int]:
    return (int(rect["x"] + rect["width"]/2), int(rect["y"] + rect["height"]/2))

def viewport_rect(driver):
    try:
        return driver.execute_script("mobile: viewportRect")
    except Exception:
        return {"left":0, "top":0, "width":2560, "height":1600}

def viewport_center(driver) -> Tuple[int, int]:
    r = viewport_rect(driver); return r["width"] // 2, r["height"] // 2

META_SHIFT = 1
KEYCODE_MAP = {**{str(d):7+d for d in range(10)},
               **{chr(o):29+i for i,o in enumerate(range(ord('a'),ord('z')+1))},
               '.':56,'-':69,'=':70,'@':77}
KEYCODE_SHIFTED = {'_':(69,META_SHIFT), '+':(70,META_SHIFT)}

def _adb_escape(text:str)->str:
    t = text.replace(" ","%s").replace("@","%40")
    specials = ['&','|','<','>','(',')',';','*','#','$','!','"',"'",':','{','}','[',']','?','\\','/']
    return "".join(("\\"+c) if c in specials else c for c in t)

def type_keycodes(driver, text:str):
    for ch in text:
        if 'A' <= ch <= 'Z':
            driver.press_keycode(KEYCODE_MAP[ch.lower()], META_SHIFT)
        elif ch in KEYCODE_MAP:
            driver.press_keycode(KEYCODE_MAP[ch])
        elif ch in KEYCODE_SHIFTED:
            code, meta = KEYCODE_SHIFTED[ch]; driver.press_keycode(code, meta)
        else:
            driver.execute_script("mobile: shell", {"command":"text","args":[_adb_escape(ch)]})
        time.sleep(0.02)
    time.sleep(0.06)

def clear_active_field(driver, label: str):
    try:
        for _ in range(64):
            driver.execute_script("mobile: shell", {"command":"input","args":["keyevent","67"]})
            time.sleep(0.008)
        time.sleep(0.12)
        logger.info(f"{label}: cleared via DEL x64")
    except Exception as e:
        logger.warning(f"{label}: clearing via DEL failed: {e}")

def type_single_shot_active(driver, text: str, label: str):
    try:
        driver.execute_script("mobile: type", {"text": text})
        logger.info(f"{label}: typed via mobile:type"); return
    except Exception as e:
        logger.warning(f"{label}: mobile:type failed: {e}")
    try:
        driver.execute_script("mobile: shell", {"command":"input","args":["text", _adb_escape(text)]})
        logger.info(f"{label}: typed via adb input text"); return
    except Exception as e:
        logger.warning(f"{label}: adb input text failed: {e}")
    try:
        type_keycodes(driver, text)
        logger.info(f"{label}: typed via keycodes"); return
    except Exception as e:
        logger.warning(f"{label}: keycodes failed: {e}")

def focus_by_bounds(driver, center_xy:Tuple[int,int], label: str):
    x,y = center_xy
    tap_ui_xy(driver, x, y)
    for _ in range(6):
        if keyboard_shown(driver): break
        time.sleep(0.12)
    tap_ui_xy(driver, x, y)
    time.sleep(0.15)
    logger.info(f"{label}: focused (bounds tap)")


def build_driver():
    from appium.options.common import AppiumOptions
    opts = AppiumOptions()
    caps = {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:deviceName": DEVICE_NAME,
        "appium:appPackage": APP_PACKAGE,
        "appium:appActivity": APP_ACTIVITY,
        "appium:noReset": True,
        "appium:newCommandTimeout": 300,
        "appium:autoGrantPermissions": True,
        "appium:disableWindowAnimation": True,
        "appium:unicodeKeyboard": True,
        "appium:resetKeyboard": True,
        "appium:connectHardwareKeyboard": False,
        "appium:adbExecTimeout": 20000,
        "appium:uiautomator2ServerLaunchTimeout": 20000,
        "appium:uiautomator2ServerInstallTimeout": 20000,
    }
    for k, v in caps.items():
        opts.set_capability(k, v)
    driver = webdriver.Remote(SERVER_URL, options=opts)

    try: driver.implicitly_wait(1)
    except Exception: pass

    try:
        driver.execute_script("mobile: shell", {"command":"ime","args":["enable","io.appium.settings/.UnicodeIME"]})
        driver.execute_script("mobile: shell", {"command":"ime","args":["set","io.appium.settings/.UnicodeIME"]})
        driver.execute_script("mobile: shell", {"command":"settings","args":["put","secure","show_ime_with_hard_keyboard","1"]})
    except Exception:
        pass

    try:
        for setting in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
            driver.execute_script("mobile: shell", {"command":"settings", "args":["put", "global", setting, "0"]})
    except Exception:
        pass

    return driver

def relaunch_app(driver):
    for fn in (
        lambda: driver.execute_script("mobile: startActivity", {"appPackage": APP_PACKAGE, "appActivity": APP_ACTIVITY}),
        lambda: driver.activate_app(APP_PACKAGE),
        lambda: driver.execute_script("mobile: shell", {"command":"am","args":["start","-n", f"{APP_PACKAGE}/{APP_ACTIVITY}"]}),
    ):
        try: fn(); return
        except Exception: pass
    raise RuntimeError("launch failed")

def clear_app_data(driver):
    try: driver.terminate_app(APP_PACKAGE); time.sleep(0.5)
    except Exception: pass
    try: driver.execute_script("mobile: shell", {"command":"pm","args":["clear", APP_PACKAGE]}); time.sleep(0.5)
    except Exception: pass


def _label(el) -> str:
    try: t = (el.get_attribute("text") or "")
    except Exception: t = ""
    try: d = (el.get_attribute("content-desc") or "")
    except Exception: d = ""
    return f"{t} {d}".strip()

def _looks_like_true_submit(label: str) -> bool:
    u = (label or "").upper()
    return ("SIGN IN" in u) and ("WITH" not in u)

def find_bottom_auth_signin(driver) -> Optional[object]:
    try:
        xp = ("//*[contains(translate(@text,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'SIGN IN') "
              "or contains(translate(@content-desc,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'SIGN IN')]")
        nodes = driver.find_elements(AppiumBy.XPATH, xp)
        if not nodes: return None
        candidates = [el for el in nodes if _looks_like_true_submit(_label(el))]
        if not candidates: return None
        candidates.sort(key=lambda e: e.rect.get("y", 0))
        return candidates[-1]
    except Exception:
        return None

def left_login_screen(driver) -> bool:
    try:
        if driver.find_elements(*S["home_marker"][0]): return True
    except Exception: pass
    try:
        if driver.find_elements(*S["festival_tab"][0]): return True
    except Exception: pass
    return False


def dismiss_inapp_notification_modal(driver) -> bool:
    try:
        btn_xps_dont = [
            "//*[@text=\"Don't allow\"]", "//*[@text=\"Don’t allow\"]",
            "//*[contains(translate(@text,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DON') and contains(translate(@text,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'ALLOW')]",
            "//*[@content-desc=\"Don't allow\" or @content-desc=\"Don’t allow\"]",
            "//android.widget.Button[contains(translate(@text,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DON') and contains(translate(@text,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'ALLOW')]",
            "//android.view.View[@content-desc=\"Don't allow\" or @content-desc=\"Don’t allow\"]",
        ]
        btn_xps_allow = [
            "//*[@text='Allow']", "//*[@content-desc='Allow']",
            "//android.widget.Button[@text='Allow' or @content-desc='Allow']",
            "//android.view.View[@content-desc='Allow']",
        ]
        for group_name, group in (("Don't allow", btn_xps_dont), ("Allow", btn_xps_allow)):
            for xp in group:
                els = driver.find_elements(AppiumBy.XPATH, xp)
                if els:
                    el = sorted(els, key=lambda e: e.rect.get("y", 0))[-1]
                    try: el.click()
                    except Exception:
                        r = el.rect
                        cx = int(r["x"] + r["width"]/2); cy = int(r["y"] + r["height"]/2)
                        tap_ui_xy(driver, cx, cy)
                    logger.info(f"In-app notif modal dismissed via '{group_name}'.")
                    time.sleep(0.2)
                    return True
        cx, cy = viewport_center(driver)
        for dy in (-40, 0, 40, 90):
            tap_ui_xy(driver, cx, cy + dy); time.sleep(0.1)
        logger.info("In-app notif modal dismissed via centerline taps (heuristic).")
        return True
    except Exception:
        return False

def handle_notifications_system(driver):
    try:
        for loc in (NOTIF_DENY, NOTIF_ALLOW):
            try:
                WebDriverWait(driver, 0.8).until(EC.element_to_be_clickable(loc)).click()
                logger.info("Dismissed system notification dialog.")
                time.sleep(0.1)
                return
            except Exception:
                continue
    except Exception:
        pass

def click_bottom_auth_signin(driver, wait_seconds: int = 10) -> bool:
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        el = find_bottom_auth_signin(driver)
        if el:
            try:
                driver.execute_script("mobile: clickGesture", {"elementId": el.id})
            except Exception:
                r = el.rect
                tap_ui_xy(driver, int(r["x"] + r["width"]/2), int(r["y"] + r["height"]/2))
            time.sleep(0.4)
            if left_login_screen(driver):
                logger.info("Bottom 'SIGN IN' submitted; left login screen.")
                return True
            if dismiss_inapp_notification_modal(driver):
                logger.info("Dismissed in-app notifications modal post-submit.")
                return True
            if not find_bottom_auth_signin(driver):
                logger.info("Submit button disappeared after click—proceeding.")
                return True
        time.sleep(0.15)

    try:
        driver.execute_script("mobile: shell", {"command":"input","args":["keyevent","66"]})
        time.sleep(0.4)
        if left_login_screen(driver) or dismiss_inapp_notification_modal(driver):
            logger.info("Submitted via ENTER fallback.")
            return True
    except Exception:
        pass
    return False


def ensure_on_festival(driver, attempts: int = 4) -> bool:
    for _ in range(attempts):
        if wait_click_any(driver, S["festival_tab"], timeout=1):
            time.sleep(0.15)
        try:
            if driver.find_elements(*S["festival_tab"][0]):
                return True
        except Exception:
            pass
        r = viewport_rect(driver)
        y = r["height"] - 110
        for x in (int(r["width"]*0.86), int(r["width"]*0.90), int(r["width"]*0.94)):
            tap_ui_xy(driver, x, y); time.sleep(0.12)
            try:
                if driver.find_elements(*S["festival_tab"][0]):
                    return True
            except Exception:
                pass
        time.sleep(0.12)
    logger.info("Could not positively confirm Festival tab; proceeding best-effort.")
    return False


def login_flow(driver, email: str, password: str):
    logger.info("Login flow: Log in → Use password → enter creds → bottom SIGN IN")

    if on_welcome(driver):
        if not wait_click_any(driver, S["welcome_login"]):
            raise TimeoutException("Log in button not clickable")
        step_pause()

    handle_notifications_system(driver)

    if not wait_click_any(driver, S["use_password"]):
        raise TimeoutException("Use password not clickable")
    step_pause()

    focus_by_bounds(driver, EMAIL_BOUNDS_CENTER, "EMAIL")
    clear_active_field(driver, "EMAIL")
    type_single_shot_active(driver, email, "EMAIL")
    time.sleep(0.25)

    try:
        driver.execute_script("mobile: shell", {"command":"input","args":["keyevent","61"]})
        time.sleep(0.12)
    except Exception:
        pass

    focus_by_bounds(driver, PWD_BOUNDS_CENTER, "PASSWORD")
    clear_active_field(driver, "PASSWORD")
    type_single_shot_active(driver, password, "PASSWORD")
    time.sleep(0.25)
    hide_keyboard(driver)

    if not click_bottom_auth_signin(driver, wait_seconds=10):
        raise TimeoutException("Could not click the bottom 'SIGN IN' (auth submit)")

    handle_notifications_system(driver)
    if dismiss_inapp_notification_modal(driver):
        logger.info("Post-login in-app modal handled.")

    WebDriverWait(driver, 6).until(EC.presence_of_element_located(S["home_marker"][0]))
    logger.info("Login succeeded (home marker found).")

def festival_flow(driver):
    ensure_on_festival(driver)
    logger.info(f"Festival: tapping {ENTER_TAPS}× (phase1 {LOWER_PHASE_TAPS} lower, then OG coords) ...")
    time.sleep(0.15)

    lower_xy = (ENTER_BUTTON_COORDS[0], ENTER_BUTTON_COORDS[1] + LOWER_PHASE_Y_OFFSET)

    for i in range(1, ENTER_TAPS + 1):
        if i <= LOWER_PHASE_TAPS:
            tx, ty = lower_xy
        else:
            tx, ty = ENTER_BUTTON_COORDS

        try:
            tap_ui_xy(driver, tx, ty)
        except Exception as e:
            logger.error(f"Enter tap {i} failed: {e}")

        if i % 25 == 0:
            logger.info(f"Enter taps: {i}/{ENTER_TAPS}")

        time.sleep(CLICK_INTERVAL)

    time.sleep(WAIT_AFTER_TAPS)

def logout_flow(driver):
    logger.info("Logging out...")
    if not wait_click_any(driver, S["menu_icon_top_left"]):
        raise TimeoutException("Menu icon not clickable")
    step_pause()
    swipe_xy(driver, *SWIPE_LOGOUT_START, *SWIPE_LOGOUT_END, SWIPE_DURATION_MS)
    step_pause()
    if not wait_click_any(driver, S["logout_button"]):
        raise TimeoutException("Logout button not clickable")
    step_pause()


def resolve_csv_path() -> Path:
    if CSV_PATH_ABSOLUTE:
        p = Path(CSV_PATH_ABSOLUTE).expanduser()
        logger.info(f"CSV set explicitly: {p}")
        return p
    script_dir = Path(__file__).resolve().parent
    candidates = [script_dir / "accountInfo.csv", Path.cwd() / "accountInfo.csv"]
    logger.info(f"Looking for accountInfo.csv in: {', '.join(str(c) for c in candidates)}")
    for c in candidates:
        if c.exists(): return c
    raise FileNotFoundError("accountInfo.csv not found. Place it next to androidBot.py or set CSV_PATH_ABSOLUTE.")

def load_accounts(csv_path: Path):
    rows = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        peek = f.read(); f.seek(0)
        try:
            has_header = csv.Sniffer().has_header(peek)
        except Exception:
            has_header = True
        if has_header:
            reader = csv.DictReader(f)
            for r in reader:
                e = (r.get("email") or r.get("Email") or r.get("EMAIL") or "").strip()
                p = (r.get("password") or r.get("Password") or r.get("PASSWORD") or "").strip()
                if e and p: rows.append((e, p))
        else:
            f.seek(0)
            for r in csv.reader(f):
                if not r: continue
                e = (r[0] or "").strip()
                p = (r[1] or "").strip() if len(r) > 1 else ""
                if e and p: rows.append((e, p))
    if not rows:
        raise ValueError("CSV parsed but no (email,password) rows found.")
    return rows

def main():
    logger.info("Starting Global Citizen automation...")

    try:
        csv_path = resolve_csv_path()
        logger.info(f"Using CSV: {csv_path}")
    except Exception as e:
        logger.error(e); sys.exit(1)

    try:
        accounts = load_accounts(csv_path)
        logger.info(f"Loaded {len(accounts)} accounts.")
    except Exception as e:
        logger.error(f"Failed to load CSV: {e}"); sys.exit(1)

    start_idx = max(1, int(START_FROM_INDEX))
    if start_idx > len(accounts):
        logger.error(f"START_FROM_INDEX ({start_idx}) is beyond CSV length ({len(accounts)}). Exiting.")
        sys.exit(1)
    accounts_to_run = accounts[start_idx - 1:]
    logger.info(f"Starting from CSV row #{start_idx}. Accounts to process: {len(accounts_to_run)}.")

    driver = None
    try:
        driver = build_driver()

        total = len(accounts_to_run)
        ok = 0
        for offset, (email, password) in enumerate(accounts_to_run, 0):
            idx = start_idx + offset
            logger.info(f"--- Account {idx}/{len(accounts)} ---")
            logger.info(f"=== Starting account: {email} ===")
            try:
                relaunch_app(driver); step_pause()
                handle_notifications_system(driver)

                if on_welcome(driver):
                    logger.info("At welcome screen.")

                login_flow(driver, email, password)

                festival_flow(driver)
                logout_flow(driver)
                ok += 1
            except Exception as e:
                logger.error(f"[{email}] flow error: {e}")
                screenshot_artifacts(driver, f"error_{idx}")
            finally:
                try: clear_app_data(driver)
                except Exception: pass
                step_pause()

        logger.info(f"Done. Success: {ok}/{total} (from row {start_idx} of {len(accounts)} total).")

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        if driver: screenshot_artifacts(driver, "fatal")
        sys.exit(2)
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

if __name__ == "__main__":
    main()
