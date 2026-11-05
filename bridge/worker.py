"""
Worker: the program that really touches the hardware. bridge_server.py starts it (you do not run it yourself).

Why a separate program?  PsychoPy opens a full screen window, Titta and Selenium are heavy, and a crash in any of
them must not kill the bridge. The bridge and the worker talk through two text lines of JSON ("pipes"):

  bridge -> worker  (worker's stdin)   {"cmd": "calibrate"}
                                       {"cmd": "familiarize", "url": "https://...", "duration": 60}
                                       {"cmd": "run_task", "index": 0, "url": "https://...", "text": "Find...", "duration": 60, "dir": "...\\task_1"}
                                       {"cmd": "stop"}      skip the running familiarization / end the running task now (handled at once)
                                       {"cmd": "quit"}      shut down
  worker -> bridge  (worker's stdout)  {"event": "ready", "tracker": "Tobii Pro Nano"}
                                       {"event": "calibration_progress", "point": 1, "total": 9}   (fake only)
                                       {"event": "calibration_done", "quality": null, "avgErrorDeg": null}
                                       {"event": "familiarization_started", "duration": 60}
                                       {"event": "intro_shown"}                   the participant sees the intro, waits for Next
                                       {"event": "familiarization_done"}
                                       {"event": "task_ready", "index": 0}        the participant sees the task, waits for Start Task
                                       {"event": "task_started", "index": 0, "duration": 60}   the recording really began
                                       {"event": "task_finished", "reason": "timeout" | "next" | "done"}   done = the participant closed the Chrome window
                                       {"event": "error", "message": "..."}   something failed, the worker keeps running
                                       {"event": "fatal", "message": "..."}   the hardware could not start, the worker exits

Everything else that gets printed (Titta, PsychoPy, our own prints) goes to stderr, so it shows in the bridge's
terminal but never mixes with the JSON lines.
"""

import argparse
import json
import queue
import sys
import threading
import traceback

# stdout is the line to the bridge. From here on, print() goes to stderr instead.
PROTOCOL = sys.stdout
sys.stdout = sys.stderr


def emit(event, **data):
    PROTOCOL.write(json.dumps({"event": event, **data}) + "\n")
    PROTOCOL.flush()


def load_hardware(name, args):
    if name == "fake":
        from hardware_fake import FakeHardware

        return FakeHardware(args.dir, speed=args.speed)
    if name == "tobii":
        from hardware_tobii import TobiiHardware

        return TobiiHardware(args.dir)
    raise ValueError(f"Unknown hardware: {name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hardware", required=True, choices=["fake", "tobii"])
    parser.add_argument("--dir", required=True, help="the folder of this session")
    parser.add_argument("--speed", type=float, default=1.0, help="fake only")
    args = parser.parse_args()

    try:
        hardware = load_hardware(args.hardware, args)
    except BaseException as problem:  # also ImportError: a library is missing in this Python
        traceback.print_exc()
        emit("fatal", message=f"{type(problem).__name__}: {problem}")
        return 1
    emit("ready", tracker=hardware.tracker_name)

    commands = queue.Queue()
    stop_event = threading.Event()
    quit_event = threading.Event()  # the session is closing: nothing may wait for the participant any more
    hardware.quit_event = quit_event

    # A second thread reads stdin, because the main thread is busy (a task can run for minutes) and "stop" must work at once.
    def read_commands():
        for line in sys.stdin:
            try:
                command = json.loads(line)
            except ValueError:
                continue
            if command.get("cmd") == "stop":
                stop_event.set()
            else:
                if command.get("cmd") == "quit":
                    quit_event.set()
                    stop_event.set()
                commands.put(command)
        quit_event.set()
        stop_event.set()
        commands.put({"cmd": "quit"})  # stdin closed = the bridge is gone: clean up and leave

    threading.Thread(target=read_commands, daemon=True).start()

    try:
        while True:
            try:
                command = commands.get(timeout=0.03)
            except queue.Empty:  # no command: the hardware may keep a window of its own alive (the head view)
                idle = getattr(hardware, "idle", None)
                if idle:
                    try:
                        idle()
                    except Exception:
                        traceback.print_exc()
                continue
            kind = command.get("cmd")
            if kind == "quit":
                break
            try:
                if kind == "calibrate":
                    result = hardware.calibrate(lambda point, total: emit("calibration_progress", point=point, total=total))
                    emit("calibration_done", **result)
                elif kind == "familiarize":
                    stop_event.clear()
                    hardware.familiarize(command["url"], command["duration"], stop_event, emit)
                    emit("familiarization_done")
                elif kind == "run_task":
                    stop_event.clear()
                    reason = hardware.run_task(
                        command["index"],
                        command["url"],
                        command.get("text", ""),
                        command["duration"],
                        stop_event,
                        command["dir"],
                        emit,
                    )
                    emit("task_finished", reason=reason)
                else:
                    emit("error", message=f"Unknown command: {kind}")
            except Exception as problem:  # a failed step must not kill the worker
                traceback.print_exc()
                emit("error", message=f"{type(problem).__name__}: {problem}")
    finally:
        try:
            hardware.close()
        except Exception:
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
