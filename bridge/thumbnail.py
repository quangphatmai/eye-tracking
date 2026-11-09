"""
The picture for a study card on the home page: a screenshot of the first website of the study (the URL of Task 1).

A Chrome window runs in the background (headless, nobody sees it), opens the page like a normal 1280x800 browser, waits a
moment for the images, takes the screenshot and shrinks it to a small JPEG. The result is returned as a data URL
("data:image/jpeg;base64,...") that the website keeps next to the study.

Used by bridge_server.py (message "capture_thumbnail"). It is slow (a few seconds), so the bridge runs it in a thread.
"""

import base64
import threading
import time

import cv2
import numpy as np

WINDOW_WIDTH, WINDOW_HEIGHT = 1280, 800  # a 16:10 window, the same shape as the card's picture
OUT_WIDTH = 640  # the picture on the card is small: half size is plenty and keeps the file at about 50 KB
JPEG_QUALITY = 72
PAGE_TIMEOUT = 20  # seconds to wait for the page; a slow page is photographed as it is
SETTLE_SECONDS = 0.8  # after the page is loaded: lazy images, fade-in animations
LOAD_WAIT = 5  # at most this long for the rest of the page (images, scripts) after the first content is there
IDLE_QUIT_SECONDS = 20  # the background Chrome stays open for the next picture and closes when nobody asks for a while

_lock = threading.Lock()  # one picture at a time
_driver_path = None  # chromedriver is found once, then kept
_driver = None  # the background Chrome: started for the first picture, re-used for the next ones (starting it takes seconds)
_last_used = 0.0


class ThumbnailError(Exception):
    pass


def _clean_url(url):
    url = (url or "").strip()
    if not url:
        raise ThumbnailError("This study has no URL for Task 1")
    if "://" not in url:
        url = "https://" + url
    if not url.lower().startswith(("http://", "https://")):
        raise ThumbnailError("Only http and https pages can be photographed")
    return url


def to_card_picture(png_bytes):
    """The screenshot (png) -> 16:10 picture, OUT_WIDTH wide, as a JPEG data URL. The TOP of the page is kept."""
    picture = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
    if picture is None:
        raise ThumbnailError("The screenshot could not be read")
    height, width = picture.shape[:2]
    wanted_height = int(round(width * WINDOW_HEIGHT / WINDOW_WIDTH))
    if height > wanted_height:
        picture = picture[:wanted_height]  # a window taller than 16:10: keep the top
    out_height = int(round(OUT_WIDTH * WINDOW_HEIGHT / WINDOW_WIDTH))
    picture = cv2.resize(picture, (OUT_WIDTH, out_height), interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", picture, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ThumbnailError("The picture could not be saved")
    return "data:image/jpeg;base64," + base64.b64encode(jpeg.tobytes()).decode("ascii")


def _start_chrome():
    global _driver_path
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    if _driver_path is None:
        _driver_path = ChromeDriverManager().install()
    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"  # do not wait for every image and ad
    options.add_argument("--headless=new")
    options.add_argument(f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}")
    options.add_argument("--hide-scrollbars")
    options.add_argument("--force-device-scale-factor=1")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-search-engine-choice-screen")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(service=Service(_driver_path), options=options)  # a new empty profile each time
    try:
        # a headless Chrome says "HeadlessChrome" in its name and some sites then show nothing: say it is a normal Chrome
        version = driver.capabilities.get("browserVersion", "130.0.0.0")
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {"userAgent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"},
        )
    except Exception:
        pass
    return driver


def _quit_driver():
    """Close the background Chrome (the caller holds _lock)."""
    global _driver
    driver, _driver = _driver, None
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass


def _quit_when_idle():
    with _lock:
        if _driver is not None and time.time() - _last_used >= IDLE_QUIT_SECONDS - 0.5:
            _quit_driver()


def _get_driver(start_chrome):
    global _driver
    if _driver is not None:
        try:
            _driver.window_handles  # still alive?
            return _driver
        except Exception:
            _quit_driver()
    try:
        _driver = start_chrome()
    except Exception as problem:
        raise ThumbnailError(f"Chrome could not be started: {type(problem).__name__}: {problem}")
    return _driver


def capture(url, start_chrome=_start_chrome):
    """Screenshot of url -> data URL. Raises ThumbnailError. `start_chrome` is only replaced in tests."""
    global _last_used
    url = _clean_url(url)
    with _lock:
        driver = _get_driver(start_chrome)
        try:
            driver.set_page_load_timeout(PAGE_TIMEOUT)
            try:
                driver.get(url)  # returns when the first content is there ("eager")
            except Exception:
                pass  # too slow: what is on the screen now is still a good picture
            deadline = time.time() + LOAD_WAIT
            while time.time() < deadline:  # a moment for the images, but never longer than LOAD_WAIT
                try:
                    if driver.execute_script("return document.readyState") == "complete":
                        break
                except Exception:
                    break
                time.sleep(0.2)
            time.sleep(SETTLE_SECONDS)
            try:
                driver.execute_script("window.scrollTo(0, 0)")
            except Exception:
                pass
            png = driver.get_screenshot_as_png()
        except Exception as problem:
            _quit_driver()  # the next picture starts a fresh Chrome
            raise ThumbnailError(f"The page could not be photographed: {type(problem).__name__}: {problem}")
        _last_used = time.time()
        timer = threading.Timer(IDLE_QUIT_SECONDS, _quit_when_idle)
        timer.daemon = True
        timer.start()
    return to_card_picture(png)
