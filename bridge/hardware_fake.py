"""A pretend eye tracker for the worker (--hardware fake). Same methods as TobiiHardware, nothing real happens.
It exists to test the bridge <-> worker plumbing on any computer. The participant's button presses are simulated by waiting."""

import os
import threading
import time


class FakeHardware:
    tracker_name = "fake"

    def __init__(self, session_dir, speed=1.0):
        self.speed = speed
        self.quit_event = threading.Event()  # worker.py replaces it

    def calibrate(self, on_progress):
        total = 9
        for point in range(1, total + 1):
            time.sleep(0.5 / self.speed)
            on_progress(point, total)
        return {"quality": "good", "avgErrorDeg": 0.6}

    def familiarize(self, url, duration, stop_event, emit):
        emit("familiarization_started", duration=duration)
        stop_event.wait(timeout=duration / self.speed)  # "stop" = the observer skips the waiting
        if self.quit_event.is_set():
            return
        stop_event.clear()
        emit("intro_shown")
        time.sleep(1.0 / self.speed)  # the participant reads the intro and presses Next

    def run_task(self, index, url, text, duration, stop_event, out_dir, emit):
        os.makedirs(out_dir, exist_ok=True)
        emit("task_ready", index=index)
        time.sleep(1.0 / self.speed)  # the participant reads the task and presses Start Task
        emit("task_started", index=index, duration=duration)
        stopped_early = stop_event.wait(timeout=duration / self.speed)  # True when "stop" arrives first
        with open(os.path.join(out_dir, "fake_task.txt"), "w") as file:
            file.write(f"task {index + 1}: {text} {url}\n")
        return "next" if stopped_early else "timeout"

    def close(self):
        pass
