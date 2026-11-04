"""
The real hardware for the worker (--hardware tobii): Tobii Pro Nano + Titta/PsychoPy calibration + Chrome + screen recording.

This is step6_browser_session.py cut into the pieces the website asks for:
  __init__      step6 "0. Chrome" + the start of "1. Calibration"   (once per session)
  calibrate     step6 "1. Calibration"                              (the observer can repeat it)
  familiarize   NEW: the participant explores the website, then reads the intro and presses Next (no tracking, no recording)
  run_task      step6 "3. gaze" + "4. recording" + "5. start" + "6. stop + save"   (once per task)
                NEW: the participant reads the task and presses Start Task. During the task Chrome is a normal window
                (tabs, address bar, minimize / maximize / close). Closing that window = "I have finished this task".
  close         closes Chrome

Every task gets its own Chrome window: it is closed when the task ends (the participant closed it, the time ran out or the
observer pressed next) and the next task opens a new one (2-3 seconds). Nothing is recorded while it opens.

What the participant sees on top of the website (the intro and the task screens) is drawn by participant_overlay.js,
which Chrome runs on every page. The PsychoPy countdown of step6 is gone: the participant starts the task themselves.

The recording code inside is the same as step6 on purpose (it is the part that is known to work on this setup).
What changed: the URL and the duration come from the website, the files go into the task's folder, and the
live overlay for the observer (step5_websocket_server.py + test_live_overlay.py) is left out for now.

NOT TESTED WITHOUT THE HARDWARE. Things that must be checked with the Tobii plugged in are marked  # CHECK
"""

import csv
import ctypes
import glob
import json
import os
import shutil
import threading
import time

# Same as step6: the real pixel size of the screen, not the scaled one (must be set before any window exists)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import cv2
import mss
import numpy as np
import tobii_research as tr
from psychopy import visual
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from titta import Titta
from webdriver_manager.chrome import ChromeDriverManager

TOBII_MODEL = "Tobii Pro Nano"
RECORDING_FPS = 15

OVERLAY_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "participant_overlay.js")
INTRO_TEXT = (
    "You are going to be given a series of tasks. "
    "When you think you have completed the task, click the \u201cX\u201d in the top right corner of the screen."
)


def scale_factor():
    """The Windows display scaling in percent (125 = 125%), or None. step7 needs it for the scroll positions."""
    try:
        return int(ctypes.windll.shcore.GetScaleFactorForDevice(0))  # CHECK: 0 = the main screen
    except Exception:
        return None


class TobiiHardware:
    tracker_name = TOBII_MODEL

    def __init__(self, session_dir):
        self.session_dir = session_dir
        self.calib_dir = os.path.join(session_dir, "calibration_results")
        os.makedirs(self.calib_dir, exist_ok=True)

        user32 = ctypes.windll.user32
        self.screen_w, self.screen_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        print(f"Screen (real pixels): {self.screen_w}x{self.screen_h}")

        # Chrome is opened later, once per task (_open_browser). Only the driver file is fetched now.
        self.driver_path = ChromeDriverManager().install()
        self.driver = None
        self.page_url = None  # the page the participant is on (to open it again if they close the window by mistake)
        self.pending_message = None  # (text, button) of the message on the screen right now, or None
        self.known_handles = 0
        with open(OVERLAY_JS, encoding="utf-8") as file:
            self.overlay_source = file.read()
        self.quit_event = threading.Event()  # worker.py replaces it: set when the session is closing

        # step6 "1. Calibration": connect to the tracker (the calibration itself happens in calibrate())
        settings = Titta.get_defaults(TOBII_MODEL)
        settings.N_CAL_TARGETS = 9
        self.tracker = Titta.Connect(settings)
        self.tracker.init()

    def _new_window(self):
        return visual.Window(monitor="testMonitor", fullscr=True, units="pix", color=(0, 0, 0))

    # ---------- Calibration ----------

    def calibrate(self, on_progress):
        # Titta runs its own calibration screens on the participant's screen and is driven from the keyboard
        # (follow what the screen says). It does not tell us "point 5 of 9", so on_progress is not used.
        win = self._new_window()
        try:
            self.tracker.calibrate(win)
        finally:
            win.close()

        # Titta leaves its files in the current folder (= the session folder): move them to calibration_results
        for pattern in ["*.cal", "*calibration_image*.png", "*validation_image*.png"]:
            for file in glob.glob(pattern):
                shutil.move(file, os.path.join(self.calib_dir, file))
        print(f"Calibration files saved in {self.calib_dir}")

        # CHECK: does calibration made in this process stay on the tracker for run_task()? In step6 it does (same process).
        # CHECK: Titta may report the calibration quality somewhere; for now we do not report it.
        return {"quality": None, "avgErrorDeg": None}

    # ---------- What the participant sees on top of the website ----------

    def _open_browser(self, url):
        """A new, normal Chrome window (maximized, clean profile) on the participant's screen, showing url."""
        self._quit_browser()
        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--window-position=0,0")  # CHECK: the main screen = the participant's screen
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-search-engine-choice-screen")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--password-store=basic")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])  # no "controlled by automated software" bar
        options.add_experimental_option("useAutomationExtension", False)
        options.add_experimental_option("prefs", {"credentials_enable_service": False, "profile.password_manager_enabled": False})
        self.driver = webdriver.Chrome(service=Service(self.driver_path), options=options)  # a new empty profile each time
        try:
            self.driver.maximize_window()
        except Exception:
            pass
        # Every page Chrome opens from now on gets the overlay (the intro and task screens)
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": self.overlay_source})
        self.page_url = url
        self.driver.get(url)
        self.known_handles = len(self.driver.window_handles)

    def _quit_browser(self):
        driver, self.driver = self.driver, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def _window_closed(self):
        """True when the participant closed the Chrome window. It must fail 3 times in a row (one slow answer is not a close)."""
        if self.driver is None:
            return True
        for _ in range(3):
            try:
                if self.driver.window_handles:
                    return False
            except Exception:
                pass
            time.sleep(0.05)
        return True

    def _follow_tabs(self):
        """The participant may open another tab (Ctrl+click) or close one: stay on a tab that exists, so scroll and address are still logged."""
        try:
            handles = self.driver.window_handles
            if not handles:
                return
            newer = len(handles) > self.known_handles
            self.known_handles = len(handles)
            try:
                current = self.driver.current_window_handle
            except Exception:
                current = None  # the tab we were on is gone
            if newer or current not in handles:
                self.driver.switch_to.window(handles[-1])
        except Exception:
            pass  # try again at the next round

    def _js(self, script):
        """Run javascript in the page. A page that is still loading can refuse: then the answer is None."""
        try:
            return self.driver.execute_script(script)
        except Exception:
            return None

    def _show_message(self, text, button_label):
        self.pending_message = (text, button_label)
        for _ in range(50):  # the page may need a moment before the overlay can be drawn
            if self._js(f"return window.__et && window.__et.showMessage({json.dumps(text)}, {json.dumps(button_label)})"):
                return
            time.sleep(0.1)
        raise RuntimeError("The participant's screen did not show the message (is the page blocking scripts?)")

    def _hide_message(self):
        self.pending_message = None
        self._js("window.__et && window.__et.hideMessage()")

    def _wait_for_click(self, stop_event):
        """Waits until the participant presses the button of the message. False if the observer skipped it instead.
        If the participant closes the window meanwhile, a new window opens with the same message."""
        while True:
            if self._js("return window.__et && window.__et.takeClick()"):
                return True
            if stop_event.wait(0.1) or self.quit_event.is_set():
                return False
            if self._window_closed():
                self._open_browser(self.page_url)
                if self.pending_message:
                    self._show_message(*self.pending_message)

    # ---------- Familiarization ----------

    def familiarize(self, url, duration, stop_event, emit):
        self._open_browser(url)
        emit("familiarization_started", duration=duration)
        # Nothing is tracked or recorded here. "stop" = the observer skips the waiting. Closing the window ends it early too.
        deadline = time.time() + duration
        while time.time() < deadline:
            if stop_event.wait(0.1) or self.quit_event.is_set():
                break
            if self._window_closed():
                self._open_browser(url)  # the intro needs a window
                break
        if self.quit_event.is_set():
            return
        stop_event.clear()  # that skip is used up: the intro still waits for the participant

        self._show_message(INTRO_TEXT, "Next")
        emit("intro_shown")
        self._wait_for_click(stop_event)  # the observer can also skip this
        self._hide_message()
        # The window stays open: the first task uses it (the next tasks each open a new one)

    # ---------- One task ----------

    def run_task(self, index, url, text, duration, stop_event, out_dir, emit):
        os.makedirs(out_dir, exist_ok=True)

        eyetracker = tr.find_all_eyetrackers()[0]  # step6 "3. Gaze logging" (done before the participant presses Start)

        # The task in a Chrome window: after the familiarization its window is still open, otherwise a new one opens (2-3 s).
        # The participant presses Start Task when ready (nothing is recorded yet).
        if self.driver is not None and not self._window_closed():
            self._follow_tabs()
            self.page_url = url
            self.driver.get(url)
        else:
            self._open_browser(url)
        self._show_message(text or f"Task {index + 1}", "Start Task")
        emit("task_ready", index=index)
        if not self._wait_for_click(stop_event):  # the observer skipped before the participant started
            self._hide_message()
            self._quit_browser()
            return "next"
        self._hide_message()

        gaze_log, frame_log, scroll_log, nav_log = [], [], [], []
        stop_flag = threading.Event()  # ends the three logging threads
        session_start = time.time()  # the one clock of this task (step6 uses the Python clock for everything)

        def gaze_data_callback(gaze_data):
            t = time.time() - session_start
            lx, ly = gaze_data["left_gaze_point_on_display_area"]
            rx, ry = gaze_data["right_gaze_point_on_display_area"]
            lvalid = gaze_data["left_gaze_point_validity"]
            rvalid = gaze_data["right_gaze_point_validity"]
            gaze_log.append((t, lx, ly, lvalid, rx, ry, rvalid))

        video_path = os.path.join(out_dir, "session_recording.mp4")

        def record_screen():  # step6 "4. Screen recording"
            with mss.mss() as sct:
                monitor = sct.monitors[1]  # the main screen = the participant's screen
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(video_path, fourcc, RECORDING_FPS, (monitor["width"], monitor["height"]))
                frame_interval = 1.0 / RECORDING_FPS
                next_frame_time = session_start
                while not stop_flag.is_set():
                    now = time.time()
                    if now >= next_frame_time:
                        img = np.array(sct.grab(monitor))
                        frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                        writer.write(frame)
                        frame_log.append(now - session_start)
                        next_frame_time += frame_interval
                    else:
                        time.sleep(0.002)
                writer.release()

        def log_scroll():  # scroll position of the page over time
            while not stop_flag.is_set():
                try:
                    scroll_y = self.driver.execute_script(
                        "return (window.pageYOffset || document.documentElement.scrollTop || 0) * (window.devicePixelRatio || 1);"
                    )
                    scroll_log.append((time.time() - session_start, scroll_y))
                except Exception:
                    pass
                time.sleep(0.1)

        def log_navigation():  # every time the page address changes
            last_url = None
            while not stop_flag.is_set():
                try:
                    current_url = self.driver.current_url
                    if current_url != last_url:
                        nav_log.append((time.time() - session_start, current_url))
                        last_url = current_url
                except Exception:
                    pass
                time.sleep(0.2)

        threads = [threading.Thread(target=record_screen), threading.Thread(target=log_scroll),
                   threading.Thread(target=log_navigation)]

        # step6 "5. Start recording"
        reason = "timeout"
        eyetracker.subscribe_to(tr.EYETRACKER_GAZE_DATA, gaze_data_callback, as_dictionary=True)
        try:
            for thread in threads:
                thread.start()
            emit("task_started", index=index, duration=duration)  # the website's timer starts now

            # step6 waited with time.sleep(SESSION_SECONDS). Here the wait also ends when the participant closes the
            # Chrome window ("done"). Running out of time = "timeout". "next" only happens when the session is closing.
            deadline = time.time() + duration
            while time.time() < deadline:
                self._follow_tabs()
                if stop_event.wait(0.1) or self.quit_event.is_set():
                    reason = "next"
                    break
                if self._window_closed():
                    reason = "done"
                    break
        finally:
            # step6 "6. Stop": always, even when something failed above
            eyetracker.unsubscribe_from(tr.EYETRACKER_GAZE_DATA, gaze_data_callback)
            stop_flag.set()
            for thread in threads:
                if thread.is_alive():
                    thread.join()

        self._save(out_dir, gaze_log, frame_log, scroll_log, nav_log)
        self._quit_browser()  # the window is closed after the recording stopped (timeout / next) or already gone (done)
        return reason

    def _save(self, out_dir, gaze_log, frame_log, scroll_log, nav_log):
        print(f"{len(gaze_log)} gaze samples, {len(frame_log)} video frames, saving in {out_dir}")

        with open(os.path.join(out_dir, "gaze_data.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["elapsed_sec", "left_x", "left_y", "left_valid", "right_x", "right_y", "right_valid"])
            writer.writerows(gaze_log)

        with open(os.path.join(out_dir, "frame_log.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["frame_index", "elapsed_sec"])
            writer.writerows(enumerate(frame_log))

        with open(os.path.join(out_dir, "scroll_log.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["elapsed_sec", "scroll_y"])
            writer.writerows(scroll_log)

        with open(os.path.join(out_dir, "nav_log.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["elapsed_sec", "url"])
            writer.writerows(nav_log)

        with open(os.path.join(out_dir, "screen_info.json"), "w") as f:
            # SCALE_PERCENT is new: step7 has SCROLL_DPI_SCALE = 1.25 typed in by hand, which is wrong on another computer
            json.dump({"SCREEN_W": self.screen_w, "SCREEN_H": self.screen_h, "SCALE_PERCENT": scale_factor()}, f)

    def close(self):
        self._quit_browser()
