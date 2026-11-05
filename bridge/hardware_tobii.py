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
import math
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
from psychopy import event, visual
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from titta import Titta
from window_mover import move_app_to_second_screen
from webdriver_manager.chrome import ChromeDriverManager

TOBII_MODEL = "Tobii Pro Nano"
RECORDING_FPS = 15
HEAD_EVERY = 0.1  # seconds between two head position samples that are saved (10 a second)
HEAD_WINDOW_SIZE = 420  # pixels: the small head window on the observer's screen
HEAD_COLORS = {"ok": "#4ba23f", "warn": "#d99a00", "bad": "#e15230"}
HEAD_TEXT = {"ok": "Head OK", "warn": "Check the position", "bad": "Out of range"}

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


def _user_position(sample, side):
    """Where one eye is in the tracker's track box: [x, y, z], each 0 to 1 (0.5 = the middle of the box, z = distance).
    `sample` is what Titta's buffer gives for its 'positioning' stream (the one its head circles are drawn with).
    None when that eye is not seen."""
    try:
        x, y, z = (float(sample[f"{side}_user_position_{axis}"][0]) for axis in "xyz")
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if math.isnan(x) or math.isnan(y) or math.isnan(z):
        return None
    return [round(x, 3), round(y, 3), round(z, 3)]


def _judge_head(left, right):
    """"ok", "warn" (a bit off) or "bad" (out of range or no eyes) for the two eyes of _user_position."""
    eyes = [eye for eye in (left, right) if eye]
    if not eyes:
        return "bad"
    x, y, z = (sum(eye[axis] for eye in eyes) / len(eyes) for axis in range(3))
    offset = math.hypot(x - 0.5, y - 0.5)
    if offset > 0.3 or z < 0.25 or z > 0.75:
        return "bad"
    if offset > 0.2 or z < 0.35 or z > 0.65 or len(eyes) == 1:
        return "warn"
    return "ok"


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

        # Head position for the observer: Titta's own head drawing in a small window on the second screen (_open_head_window)
        self.head_window = None
        self.head_picture = None
        self.head_text = None
        self.head_failed = False
        self.head_bad_since = None  # when the head went out of range (None = it is not)
        self.head_last = 0.0
        self.head_sink = None  # (list, start time) while a task records: the samples are also saved in head_position.csv

        # step6 "1. Calibration": connect to the tracker (the calibration itself happens in calibrate())
        settings = Titta.get_defaults(TOBII_MODEL)
        settings.N_CAL_TARGETS = 9
        self.tracker = Titta.Connect(settings)
        self.tracker.init()

    def _new_window(self):
        return visual.Window(monitor="testMonitor", fullscr=True, units="pix", color=(0, 0, 0))

    def _send_results_to_observer(self):
        """The calibration is exactly Titta's normal one (one window, the participant's screen). Only its RESULTS screen
        (the validation image and the Accept / Recalibrate buttons) is moved to the observer's screen (the second one):
        the participant sees "Please wait..." meanwhile. During the setup and the dots nothing is drawn on the observer's
        screen: no mirroring of the participant's eyes, so no lag. With one screen everything stays as it was."""
        tracker = self.tracker
        if getattr(tracker, "_results_moved", False) or not hasattr(tracker, "_show_validation_screen"):
            return
        original = tracker._show_validation_screen

        def use_stimuli_on(operator, window):
            """Titta draws its buttons and images in tracker.win_temp: build them again for that window (dual screen mode)."""
            dot, animator = tracker.cal_dot, getattr(tracker, "animator", None)  # the dots of the calibration stay as they are
            tracker.win_operator = operator
            tracker.win_temp = window
            tracker.mouse = event.Mouse(win=window)
            tracker.mouse.setVisible(0)
            tracker._create_calibration_buttons()
            tracker.cal_dot = dot
            if animator is not None:
                tracker.animator = animator

        def results_on_the_observer_screen():
            operator = self._new_operator_window()
            if operator is None:
                return original()  # one screen: as before
            use_stimuli_on(operator, operator)
            try:
                return original()
            finally:
                use_stimuli_on(None, tracker.win)  # a recalibration goes on as in a single window
                operator.close()

        tracker._show_validation_screen = results_on_the_observer_screen
        tracker._results_moved = True

    def _new_operator_window(self):
        """The observer's window for the calibration, on the second screen. None when there is only one screen (or it fails)."""
        try:
            if ctypes.windll.user32.GetSystemMetrics(80) < 2:  # 80 = SM_CMONITORS: how many screens
                return None
            return visual.Window(monitor="testMonitor", fullscr=True, screen=1, units="norm", color=(0, 0, 0))  # CHECK: screen 1
        except Exception as problem:
            print(f"No observer window for the calibration: {type(problem).__name__}: {problem}")
            return None

    # ---------- Head position (for the observer) ----------

    def _open_head_window(self):
        """Titta's head and circle (the picture of the calibration's first screen) in a small window on the SECOND screen, so the
        observer sees the participant's head move live during the tasks. Nothing is drawn on the participant's screen (it is recorded)."""
        if self.head_window is not None:
            return
        try:
            if ctypes.windll.user32.GetSystemMetrics(80) < 2:  # 80 = SM_CMONITORS
                print("Head view: only one screen, so it is not shown (it would be in the recording).")
                return
            from titta import helpers_tobii as titta_helpers

            win = visual.Window(size=(HEAD_WINDOW_SIZE, HEAD_WINDOW_SIZE), fullscr=False, screen=1, units="height",
                                color=(-1, -1, -1), allowGUI=True)  # CHECK: screen 1 = the observer's screen
            settings = self.tracker.settings
            self.head_picture = titta_helpers.EThead(win, settings.HEAD_BOX_CENTER, settings.graphics.HEAD_POS_CIRCLE_FIXED_COLOR,
                                                     settings.graphics.HEAD_POS_CIRCLE_MOVING_COLOR)
            self.head_text = visual.TextStim(win, text="", pos=(0, 0.43), height=0.05, color="white", units="height")
            self.tracker.buffer.start("gaze")  # the two streams Titta's setup screen reads
            self.tracker.buffer.start("positioning")
            self.head_window = win
            self.head_failed = False
            self.head_bad_since = None
            try:  # bottom right of the observer's screen, above the other windows
                screen = win.winHandle.screen
                x = screen.x + screen.width - HEAD_WINDOW_SIZE - 40
                y = screen.y + screen.height - HEAD_WINDOW_SIZE - 120
                # -1 = HWND_TOPMOST, 0x1 = SWP_NOSIZE, 0x40 = SWP_SHOWWINDOW
                ctypes.windll.user32.SetWindowPos(win.winHandle._hwnd, -1, int(x), int(y), 0, 0, 0x1 | 0x40)
                win.winHandle.set_caption("Head position (observer only)")
            except Exception as problem:
                print(f"Head window not placed: {type(problem).__name__}: {problem}")
        except Exception as problem:  # a help for the observer: it must never stop a session
            print(f"Head view not available: {type(problem).__name__}: {problem}")
            self._close_head_window()

    def _close_head_window(self):
        window, self.head_window = self.head_window, None
        self.head_picture = None
        if window is not None:
            for stream in ("positioning", "gaze"):
                try:
                    self.tracker.buffer.stop(stream)
                except Exception:
                    pass
            try:
                window.close()
            except Exception:
                pass

    def _head_tick(self):
        """Draws one frame of the head view (also saves it during a task).
        PsychoPy windows must be used from the main thread, so this is called by every loop that waits (about 30 a second)."""
        window = self.head_window
        if window is None:
            return
        try:
            buffer = self.tracker.buffer
            sample = buffer.peek_N("gaze", 1)
            position = buffer.peek_N("positioning", 1)
            if len(sample["left_pupil_diameter"]) == 0 or len(position["left_user_position_x"]) == 0:
                return  # no sample yet
            self.head_picture.update(sample, position, eye="both")
            left, right = _user_position(position, "left"), _user_position(position, "right")

            now = time.time()
            if now - self.head_last >= HEAD_EVERY:
                self.head_last = now
                if self.head_sink is not None:
                    self.head_sink[0].append((now - self.head_sink[1], left, right))

            state = _judge_head(left, right)
            if state == "bad":
                self.head_bad_since = self.head_bad_since or now
                if now - self.head_bad_since < 0.6:
                    state = "warn"  # a blink loses the eyes for a moment: not "out of range" yet
            else:
                self.head_bad_since = None

            self.head_picture.static_circ.lineColor = HEAD_COLORS[state]
            self.head_picture.draw()
            self.head_text.text = HEAD_TEXT[state]
            self.head_text.color = HEAD_COLORS[state]
            self.head_text.draw()
            window.flip()
        except Exception as problem:
            if not self.head_failed:  # say it once, then keep quiet: the session goes on without the head view
                self.head_failed = True
                print(f"Head view stopped: {type(problem).__name__}: {problem}")

    def idle(self):
        """worker.py calls this about 30 times a second while no command runs (so the head window stays alive)."""
        self._head_tick()

    # ---------- Calibration ----------

    def calibrate(self, on_progress):
        # Titta runs its own calibration screens on the participant's screen and is driven from the keyboard (the head circles,
        # Space, the dots). It does not tell us "point 5 of 9", so on_progress is not used. With two screens only the RESULTS
        # screen goes to the observer's screen (see _send_results_to_observer).
        self._close_head_window()  # Titta uses the tracker by itself during the calibration (and nothing may mirror it)
        move_app_to_second_screen()  # the calibration covers the main screen: the observer's page goes to the other one first
        self._send_results_to_observer()
        win = self._new_window()
        try:
            self.tracker.calibrate(win)
        finally:
            win.close()
        self._open_head_window()  # from now on the observer sees the head live

        # Titta leaves its files in the current folder (= the session folder): move them to calibration_results
        for pattern in ["*.cal", "*calibration_image*.png", "*validation_image*.png"]:
            for file in glob.glob(pattern):
                shutil.move(file, os.path.join(self.calib_dir, file))
        print(f"Calibration files saved in {self.calib_dir}")

        # CHECK: does calibration made in this process stay on the tracker for run_task()? In step6 it does (same process).
        # Titta keeps the accuracy of the accepted calibration (degrees of the angle between gaze and target, left and right eye)
        try:
            row = self.tracker.deviations[self.tracker.selected_calibration - 1]
            error = float(sum(row[:2]) / 2)  # CHECK: row = [left accuracy, right accuracy, ...]
            if math.isnan(error):
                raise ValueError("no accuracy")
            quality = "good" if error <= 0.5 else "fair" if error <= 1.0 else "poor"
            return {"quality": quality, "avgErrorDeg": round(error, 2)}
        except Exception as problem:
            print(f"Calibration quality not available: {type(problem).__name__}: {problem}")
            return {"quality": None, "avgErrorDeg": None}

    # ---------- What the participant sees on top of the website ----------

    def _open_browser(self, url):
        """A new, normal Chrome window (maximized, clean profile) on the participant's screen, showing url."""
        self._quit_browser()
        options = webdriver.ChromeOptions()
        options.page_load_strategy = "none"  # do not wait for pages to load: a waiting driver freezes the scroll/leave logging for seconds
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
        for _ in range(300):  # (30 s) the page may need a moment before the overlay can be drawn
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
        next_check = 0.0
        while True:
            self._head_tick()
            if stop_event.wait(0.01) or self.quit_event.is_set():
                return False
            now = time.time()
            if now < next_check:  # the browser is asked 10 times a second, the head view is drawn more often
                continue
            next_check = now + 0.1
            if self._js("return window.__et && window.__et.takeClick()"):
                return True
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
        next_check = 0.0
        while time.time() < deadline:
            self._head_tick()
            if stop_event.wait(0.01) or self.quit_event.is_set():
                break
            if time.time() < next_check:
                continue
            next_check = time.time() + 0.1
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
            old_page = self._js("return window.__et && window.__et.pageId")
            self.driver.get(url)
            for _ in range(300):  # the driver does not wait for the load: wait until the NEW page is there
                if self._js("return window.__et && window.__et.pageId") not in (None, old_page):
                    break
                time.sleep(0.1)
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
        head_log = []
        self.head_sink = (head_log, session_start)

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

        def log_page():  # scroll position + page changes, from ONE poll every 0.1 s
            last_url = last_id = None
            was_leaving = expecting = False
            while not stop_flag.is_set():
                t = time.time() - session_start
                try:
                    state = self.driver.execute_script(
                        "const e = window.__et; return [(window.pageYOffset || document.documentElement.scrollTop || 0)"
                        " * (window.devicePixelRatio || 1), e && e.isLeaving ? e.isLeaving() : false,"
                        " e && e.pageId ? e.pageId : '', location.href];"
                    )
                except Exception:
                    state = None
                if state and state[2] and state[3]:  # no page id = the new page is not ready yet: wait
                    scroll_y, leaving, page_id, url = state
                    new_event = False
                    if last_id is None:
                        new_event = True
                    elif leaving and not was_leaving:
                        new_event = expecting = True  # the click: the old page's heat stops NOW
                    elif page_id != last_id or url != last_url:
                        if expecting and page_id != last_id:
                            expecting = False  # the page we already logged at the click has arrived
                        else:
                            new_event = True  # the page changed without a click (script, back button)
                    if not leaving and was_leaving and page_id == last_id:
                        expecting = False  # the leave was cancelled
                    if new_event:
                        nav_log.append((t, url))
                    if not leaving:  # while leaving, the old page is frozen: its scroll says nothing about the next page
                        scroll_log.append((t, scroll_y))
                    last_url, last_id, was_leaving = url, page_id, leaving
                time.sleep(0.1)

        threads = [threading.Thread(target=record_screen), threading.Thread(target=log_page)]

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
            next_check = 0.0
            while time.time() < deadline:
                self._head_tick()
                if stop_event.wait(0.01) or self.quit_event.is_set():
                    reason = "next"
                    break
                if time.time() < next_check:
                    continue
                next_check = time.time() + 0.1
                self._follow_tabs()
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
            self.head_sink = None

        self._save(out_dir, gaze_log, frame_log, scroll_log, nav_log, head_log)
        self._quit_browser()  # the window is closed after the recording stopped (timeout / next) or already gone (done)
        return reason

    def _save(self, out_dir, gaze_log, frame_log, scroll_log, nav_log, head_log):
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

        # NEW: where the eyes were in the track box (10 a second). Empty = that eye was not seen. Not used by step7:
        # it tells when the participant's head was out of the frame (x, y, z: 0 to 1, 0.5 = the middle of the box)
        with open(os.path.join(out_dir, "head_position.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["elapsed_sec", "left_x", "left_y", "left_z", "right_x", "right_y", "right_z"])
            for elapsed, left, right in head_log:
                writer.writerow([round(elapsed, 3)] + (left or ["", "", ""]) + (right or ["", "", ""]))

        with open(os.path.join(out_dir, "screen_info.json"), "w") as f:
            # SCALE_PERCENT is new: step7 has SCROLL_DPI_SCALE = 1.25 typed in by hand, which is wrong on another computer
            json.dump({"SCREEN_W": self.screen_w, "SCREEN_H": self.screen_h, "SCALE_PERCENT": scale_factor()}, f)

    def close(self):
        self._close_head_window()
        self._quit_browser()
