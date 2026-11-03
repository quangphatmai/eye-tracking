"""
The real hardware for the worker (--hardware tobii): Tobii Pro Nano + Titta/PsychoPy calibration + Chrome + screen recording.

This is step6_browser_session.py cut into the pieces the website asks for:
  __init__      step6 "0. Chrome" + the start of "1. Calibration"   (once per session)
  calibrate     step6 "1. Calibration"                              (the observer can repeat it)
  familiarize   NEW: the participant explores the website, then reads the intro and presses Next (no tracking, no recording)
  run_task      step6 "3. gaze" + "4. recording" + "5. start" + "6. stop + save"   (once per task)
                NEW: the participant reads the task and presses Start Task; during the task there is an X button
  close         driver.quit()

What the participant sees on top of the website (the intro, the task, the X button) is drawn by participant_overlay.js,
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
INTRO_TEXT = "You are going to be given a series of tasks. When you think you have completed the task click the \u201cX\u201d."


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

        # step6 "0. Chrome first": a kiosk Chrome on the participant's screen, waiting on a blank page
        options = webdriver.ChromeOptions()
        options.add_argument("--kiosk")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("useAutomationExtension", False)
        self.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        self.driver.get("about:blank")

        # From now on every page Chrome opens gets the overlay (intro / task screens / X button)
        with open(OVERLAY_JS, encoding="utf-8") as file:
            self.overlay_source = file.read()
        self.overlay_script_id = None
        self.quit_event = threading.Event()  # worker.py replaces it: set when the session is closing
        self._load_overlay(want_x=False)

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

    def _load_overlay(self, want_x):
        """Chrome runs the overlay on every NEW page. want_x: the X button shows by itself on every new page (during a task)."""
        if self.overlay_script_id:
            self.driver.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument", {"identifier": self.overlay_script_id})
        source = f"window.__etWantX = {'true' if want_x else 'false'};\n" + self.overlay_source
        result = self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        self.overlay_script_id = result["identifier"]

    def _follow_new_tab(self):
        """A link that opens a NEW tab: Chrome would not run the overlay there (it was added for the old tab only).
        So go to the new tab, give it the overlay and the X button too."""
        try:
            handles = self.driver.window_handles
            if len(handles) <= self.known_handles:
                return
            self.known_handles = len(handles)
            self.driver.switch_to.window(handles[-1])
            source = f"window.__etWantX = true;\n" + self.overlay_source
            result = self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": source})
            self.overlay_script_id = result["identifier"]
            self.driver.execute_script(source)  # the page that is already open in that tab
            self.driver.execute_script("window.__et && window.__et.showX(true)")
        except Exception:
            pass  # the tab may be closing: try again at the next round

    def _js(self, script):
        """Run javascript in the page. A page that is still loading can refuse: then the answer is None."""
        try:
            return self.driver.execute_script(script)
        except Exception:
            return None

    def _show_message(self, text, button_label):
        for _ in range(50):  # the page may need a moment before the overlay can be drawn
            if self._js(f"return window.__et && window.__et.showMessage({json.dumps(text)}, {json.dumps(button_label)})"):
                return
            time.sleep(0.1)
        raise RuntimeError("The participant's screen did not show the message (is the page blocking scripts?)")

    def _wait_for_click(self, stop_event):
        """Waits until the participant presses the button of the message. False if the observer skipped it instead."""
        while True:
            if self._js("return window.__et && window.__et.takeClick()"):
                return True
            if stop_event.wait(0.1) or self.quit_event.is_set():
                return False

    # ---------- Familiarization ----------

    def familiarize(self, url, duration, stop_event, emit):
        self.driver.get(url)
        emit("familiarization_started", duration=duration)
        stop_event.wait(timeout=duration)  # nothing is tracked or recorded here. "stop" = the observer skips the waiting
        if self.quit_event.is_set():
            return
        stop_event.clear()  # that skip is used up: the intro still waits for the participant

        self._show_message(INTRO_TEXT, "Next")
        emit("intro_shown")
        self._wait_for_click(stop_event)  # the observer can also skip this
        self._js("window.__et.hideMessage()")

    # ---------- One task ----------

    def run_task(self, index, url, text, duration, stop_event, out_dir, emit):
        os.makedirs(out_dir, exist_ok=True)

        eyetracker = tr.find_all_eyetrackers()[0]  # step6 "3. Gaze logging" (done before the participant presses Start)

        # The task on a fresh page, the participant presses Start Task when ready (nothing is recorded yet)
        self.driver.get(url)
        self._show_message(text or f"Task {index + 1}", "Start Task")
        emit("task_ready", index=index)
        if not self._wait_for_click(stop_event):  # the observer skipped before the participant started
            self._js("window.__et.hideMessage()")
            return "next"
        self._js("window.__et.hideMessage()")

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
        self._load_overlay(want_x=True)  # every page the participant opens during the task gets the X button
        eyetracker.subscribe_to(tr.EYETRACKER_GAZE_DATA, gaze_data_callback, as_dictionary=True)
        try:
            for thread in threads:
                thread.start()
            self._js("return window.__et.showX(true)")  # on the page that is open now
            emit("task_started", index=index, duration=duration)  # the website's timer starts now

            # step6 waited with time.sleep(SESSION_SECONDS). Here the wait also ends when the participant presses X
            # ("done") or the observer presses "next task" ("next").
            deadline = time.time() + duration
            self.known_handles = len(self.driver.window_handles)
            while time.time() < deadline:
                self._follow_new_tab()
                if stop_event.wait(0.1):
                    reason = "next"
                    break
                if self._js("return window.__et && window.__et.takeX()"):
                    reason = "done"
                    break
        finally:
            # step6 "6. Stop": always, even when something failed above
            eyetracker.unsubscribe_from(tr.EYETRACKER_GAZE_DATA, gaze_data_callback)
            stop_flag.set()
            for thread in threads:
                if thread.is_alive():
                    thread.join()
            self._load_overlay(want_x=False)
            self._js("window.__et && window.__et.showX(false)")

        self._save(out_dir, gaze_log, frame_log, scroll_log, nav_log)
        self.driver.get("about:blank")  # the participant must not keep looking at the old page between tasks
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
        try:
            self.driver.quit()
        except Exception:
            pass
