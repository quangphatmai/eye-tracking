"""
Eye-tracker bridge (the "helper").

The website cannot talk to the Tobii eye tracker, but this program can (it is Python).
So the website talks to this program, and this program talks to the hardware.

    website (browser)  <--WebSocket, JSON-->  bridge_server.py  <-->  eye tracker + screen recording

Three modes (the "backend" = what really happens when the website asks for something):
  mock   pretends everything, inside this process. For building the website without hardware.
  fake   starts the real worker process (worker.py) but with a pretend eye tracker. Tests the plumbing.
  tobii  starts the worker with the real Tobii Pro Nano + PsychoPy + Chrome + screen recording.

Run:   python bridge_server.py --mode mock
Test:  python test_bridge.py            (in a second terminal, while the server is running)

------------------------------------------------------------------------------------------------
THE MESSAGES  (every message is one JSON object with a "type")

Website -> bridge
  start_session     { studyId, participantId }          open a session for one participant
  start_calibration {}                                   run the calibration (needs a session)
  start_familiarization { url, duration }                the participant explores `url` for `duration` seconds
                                                         (5 to 300, default 60). No eye tracking, no recording.
                                                         Then the participant sees "Next", then it is over.
  start_task        { index, text, url, duration }       task `index` (0 = Task 1): the participant sees `text` and presses
                                                         "Start Task"; then it records for at most `duration` seconds (10 to 180)
  finish_task       {}                                   the observer pressed "skip / next task": end the running
                                                         familiarization or task now
  end_session       {}                                   all tasks done: save everything and close the session.
                                                         (The videos are NOT made now: that is heavy work, it must not run
                                                         while the next participant is being recorded.)
  process_participants { studyId, participants: [{ participantId, sessionId, tasks }] }
                                                         later, when nobody is being recorded: make the heatmap and
                                                         scanpath videos, one participant after the other

Bridge -> website
  ready               { version, mode, tracker }         sent once, right after the website connects
  session_started     { sessionId }
  calibration_progress{ point, total }                   "point 5 of 9" (the real calibration has no such events)
  calibration_done    { quality, avgErrorDeg }           both can be null (the real calibration does not report them)
  familiarization_started { duration }                   the participant is exploring the website
  intro_shown         {}                                 the participant sees the intro text and the "Next" button
  familiarization_done{}                                 the participant pressed "Next"
  task_ready          { index }                          the participant sees the task and the "Start Task" button
  task_started        { index, duration }                the participant pressed "Start Task": the recording really began
  task_finished       { index, reason }                  reason: "timeout" | "next" (observer) | "done" (the participant pressed X)
  session_ended       { sessionId, studyId, participantId, tasks }   the recordings are saved. tasks = how many were recorded
  participant_started { participantId }                  the videos of this participant are being made
  participant_progress{ participantId, done, total, percent }   done of total tasks are finished, percent = the current task
  participant_done    { participantId, videos }          videos: [{ task, heatmap, scanpath }] (url or null)
  participant_failed  { participantId, message }         this participant failed, the next one still gets its turn
  processing_done     {}                                 all participants were tried
  error               { code, message }                  something was refused, the connection stays open
------------------------------------------------------------------------------------------------
"""

import argparse
import asyncio
import json
import os
import re
import uuid
from urllib.parse import urlparse

VERSION = "0.5.0"
HOST = "127.0.0.1"  # this computer only. Never "0.0.0.0": that would let other computers on the network in
PORT = 8766  # 8765 is already used by step5_websocket_server.py

SESSION_ID = re.compile(r"[0-9a-f]{8}")  # what uuid4().hex[:8] gives: nothing else may become part of a folder name

MIN_DURATION = 10
MAX_DURATION = 180  # 3 minutes, the maximum the team chose
DEFAULT_DURATION = 60

# The familiarization: the participant explores the website before the tasks. No eye tracking, no recording.
MIN_FAMILIARIZATION = 5
MAX_FAMILIARIZATION = 300
DEFAULT_FAMILIARIZATION = 60

# Pages that may talk to the bridge. A browser tells us which page it comes from (the Origin header),
# so a random website cannot drive the eye tracker. Add your Netlify address with --origin.
DEFAULT_ORIGINS = [
    None,  # programs that are not a browser (test_bridge.py)
    "http://localhost:5500",  # VS Code Live Server
    "http://127.0.0.1:5500",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
]


# ---------- Backends: the part that touches the hardware ----------


class MockBackend:
    """Pretends to be the eye tracker. `speed` 10 makes every wait 10 times shorter (for tests)."""

    name = "mock"
    tracker = None  # the real backend will put the tracker's model name here

    def __init__(self, speed=1.0):
        self.speed = speed

    async def open_session(self, session):
        pass  # the real backends start the hardware here

    async def close_session(self):
        pass  # ... and stop it here

    async def calibrate(self, on_progress):
        total = 9
        for point in range(1, total + 1):
            await asyncio.sleep(0.5 / self.speed)
            await on_progress(point, total)
        return {"quality": "good", "avgErrorDeg": 0.6}

    async def familiarize(self, url, duration, stop, emit):
        """The participant explores the site; `stop` skips the waiting. Then the participant "presses Next"."""
        await emit("familiarization_started", duration=duration)
        try:
            await asyncio.wait_for(stop.wait(), timeout=duration / self.speed)
        except asyncio.TimeoutError:
            pass
        stop.clear()  # the skip is used up
        await emit("intro_shown")
        await asyncio.sleep(1.0 / self.speed)  # pretend the participant reads the intro and presses Next

    async def run_task(self, index, url, text, duration, stop, emit):
        """The participant "presses Start Task", then it records until `duration` runs out ("timeout") or `stop` is set ("next").
        (The real backend can also end with "done": the participant pressed X.)"""
        await emit("task_ready", index=index)
        await asyncio.sleep(1.0 / self.speed)  # pretend the participant reads the task and presses Start Task
        await emit("task_started", index=index, duration=duration)
        try:
            await asyncio.wait_for(stop.wait(), timeout=duration / self.speed)
            return "next"
        except asyncio.TimeoutError:
            return "timeout"

    async def process_participant(self, study_id, participant_id, session_id, task_count, on_progress):
        videos = []
        for index in range(task_count):
            for percent in (0, 50, 100):
                await asyncio.sleep(0.3 / self.speed)
                await on_progress(index, task_count, percent)
            videos.append({"task": index, "heatmap": None, "scanpath": None})  # no real videos in mock mode
        return videos


# ---------- One website connection ----------


class Connection:
    """The state of one connected website. It is independent from the network library:
    `emit` is any function that sends a dict to the website, so this class can be tested without sockets."""

    def __init__(self, emit, backend):
        self._emit = emit
        self.backend = backend
        self.session = None  # {"id", "studyId", "participantId", "calibrated", "finished": [task indexes]}
        self.job = None  # the calibration / task / processing that is running right now
        self.stop = None  # asyncio.Event: set by finish_task to end the running task early

    async def emit(self, type, **data):
        await self._emit({"type": type, **data})

    async def error(self, code, message):
        await self.emit("error", code=code, message=message)

    @property
    def busy(self):
        return self.job is not None and not self.job.done()

    async def start(self):
        await self.emit("ready", version=VERSION, mode=self.backend.name, tracker=self.backend.tracker)

    async def close(self):
        """The website went away: stop whatever is running, then let the backend shut the hardware down."""
        if self.busy:
            self.job.cancel()
            try:
                await self.job
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.backend.close_session()
            self.session = None

    # -- incoming messages --

    async def handle(self, raw):
        try:
            message = json.loads(raw)
            kind = message["type"]
        except (ValueError, KeyError, TypeError):
            return await self.error("bad_message", 'Send JSON like {"type": "start_session"}')

        handler = {
            "start_session": self.on_start_session,
            "start_calibration": self.on_start_calibration,
            "start_familiarization": self.on_start_familiarization,
            "start_task": self.on_start_task,
            "finish_task": self.on_finish_task,
            "end_session": self.on_end_session,
            "process_participants": self.on_process_participants,
        }.get(kind)
        if handler is None:
            return await self.error("unknown_type", f"Unknown message type: {kind}")
        await handler(message)

    async def on_start_session(self, message):
        if self.busy:
            return await self.error("busy", "Something is still running")
        if self.session:  # a new session replaces the old one
            await self.backend.close_session()
            self.session = None

        session = {
            "id": uuid.uuid4().hex[:8],
            "studyId": message.get("studyId"),
            "participantId": message.get("participantId"),
            "calibrated": False,
            "finished": [],
        }
        try:
            await self.backend.open_session(session)  # the real backend starts the hardware here (can take a few seconds)
        except Exception as problem:
            return await self.error("hardware_unavailable", str(problem))
        self.session = session
        await self.emit("session_started", sessionId=session["id"])

    async def on_start_calibration(self, message):
        if not self.session:
            return await self.error("no_session", "Send start_session first")
        if self.busy:
            return await self.error("busy", "Something is still running")
        self.job = asyncio.create_task(self._calibrate())

    async def on_start_familiarization(self, message):
        if not self.session:
            return await self.error("no_session", "Send start_session first")
        if not self.session["calibrated"]:
            return await self.error("not_calibrated", "Calibrate before the familiarization")
        if self.busy:
            return await self.error("busy", "Something is still running")

        url = message.get("url")
        duration = message.get("duration", DEFAULT_FAMILIARIZATION)
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https"):
            return await self.error("bad_familiarization", "url must start with http:// or https://")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            return await self.error("bad_familiarization", "duration must be a number of seconds")
        duration = max(MIN_FAMILIARIZATION, min(MAX_FAMILIARIZATION, duration))

        self.stop = asyncio.Event()
        self.job = asyncio.create_task(self._familiarize(url, duration))

    async def on_start_task(self, message):
        if not self.session:
            return await self.error("no_session", "Send start_session first")
        if not self.session["calibrated"]:
            return await self.error("not_calibrated", "Calibrate before starting a task")
        if self.busy:
            return await self.error("busy", "Something is still running")

        index = message.get("index")
        url = message.get("url")
        text = message.get("text", "")
        duration = message.get("duration", DEFAULT_DURATION)
        if not isinstance(index, int) or index < 0:
            return await self.error("bad_task", "index must be 0, 1, 2...")
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https"):
            return await self.error("bad_task", "url must start with http:// or https://")
        if not isinstance(text, str):
            return await self.error("bad_task", "text must be a string")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            return await self.error("bad_task", "duration must be a number of seconds")
        duration = max(MIN_DURATION, min(MAX_DURATION, duration))  # never longer than 3 minutes

        self.stop = asyncio.Event()
        self.job = asyncio.create_task(self._run_task(index, url, text, duration))

    async def on_finish_task(self, message):
        if self.stop is None or not self.busy:
            return await self.error("no_task", "Nothing is running")
        self.stop.set()

    async def on_end_session(self, message):
        if not self.session:
            return await self.error("no_session", "Send start_session first")
        if self.busy:
            return await self.error("busy", "Something is still running")
        self.job = asyncio.create_task(self._end_session())

    async def on_process_participants(self, message):
        if self.session:
            return await self.error("session_open", "A session is open. End it first: the videos are made when nobody is being recorded")
        if self.busy:
            return await self.error("busy", "Something is still running")

        study_id = message.get("studyId")
        entries = message.get("participants")
        if not isinstance(study_id, int) or isinstance(study_id, bool) or not isinstance(entries, list) or not entries:
            return await self.error("bad_process", "Send studyId and a list of participants")
        for entry in entries:
            ok = (
                isinstance(entry, dict)
                and isinstance(entry.get("participantId"), int)
                and isinstance(entry.get("sessionId"), str)
                and SESSION_ID.fullmatch(entry["sessionId"])
                and isinstance(entry.get("tasks"), int)
                and 0 <= entry["tasks"] <= 100
            )
            if not ok:
                return await self.error("bad_process", "Each participant needs participantId, sessionId and tasks")
        self.job = asyncio.create_task(self._process_participants(study_id, entries))

    # -- the long jobs (each one runs as its own asyncio task so the connection keeps listening) --

    async def _calibrate(self):
        async def progress(point, total):
            await self.emit("calibration_progress", point=point, total=total)

        try:
            result = await self.backend.calibrate(progress)
            self.session["calibrated"] = True
            await self.emit("calibration_done", **result)
        except asyncio.CancelledError:
            raise
        except Exception as problem:
            await self.error("calibration_failed", str(problem))

    async def _familiarize(self, url, duration):
        try:
            await self.backend.familiarize(url, duration, self.stop, self.emit)
            await self.emit("familiarization_done")
        except asyncio.CancelledError:
            raise
        except Exception as problem:
            await self.error("familiarization_failed", str(problem))

    async def _run_task(self, index, url, text, duration):
        try:
            # the backend sends task_ready and task_started itself: they depend on what the participant does
            reason = await self.backend.run_task(index, url, text, duration, self.stop, self.emit)
            self.session["finished"].append(index)
            await self.emit("task_finished", index=index, reason=reason)
        except asyncio.CancelledError:
            raise
        except Exception as problem:
            await self.error("task_failed", str(problem))

    async def _end_session(self):
        try:
            session = self.session
            await self.backend.close_session()  # the worker saves its files and closes Chrome and the eye tracker
            self.session = None
            await self.emit(
                "session_ended",
                sessionId=session["id"],
                studyId=session["studyId"],
                participantId=session["participantId"],
                tasks=len(session["finished"]),
            )
        except asyncio.CancelledError:
            raise
        except Exception as problem:
            await self.error("end_failed", str(problem))

    async def _process_participants(self, study_id, entries):
        try:
            for entry in entries:
                participant_id = entry["participantId"]
                await self.emit("participant_started", participantId=participant_id)

                async def progress(done, total, percent, participant_id=participant_id):
                    await self.emit("participant_progress", participantId=participant_id, done=done, total=total, percent=percent)

                try:
                    videos = await self.backend.process_participant(
                        study_id, participant_id, entry["sessionId"], entry["tasks"], progress
                    )
                    await self.emit("participant_done", participantId=participant_id, videos=videos)
                except asyncio.CancelledError:
                    raise
                except Exception as problem:  # one broken recording must not stop the others
                    await self.emit("participant_failed", participantId=participant_id, message=str(problem))
            await self.emit("processing_done")
        except asyncio.CancelledError:
            raise


# ---------- The network part (the only place that needs the `websockets` package) ----------


async def serve(backend, port, origins):
    import websockets  # pip install websockets  (already installed for step5_websocket_server.py)

    async def on_connect(websocket, *_):  # older versions of the library also pass a `path`
        async def emit(message):
            await websocket.send(json.dumps(message))

        connection = Connection(emit, backend)
        print(f"Website connected ({websocket.remote_address})")
        try:
            await connection.start()
            async for raw in websocket:
                await connection.handle(raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            await connection.close()
            print("Website disconnected")

    async with websockets.serve(on_connect, HOST, port, origins=origins):
        print(f"Bridge running ({backend.name} mode) at ws://{HOST}:{port}  (Ctrl+C to stop)")
        print("Allowed pages:", ", ".join(o for o in origins if o))
        await asyncio.Future()


def main():
    parser = argparse.ArgumentParser(description="Eye-tracker bridge for the website")
    parser.add_argument("--mode", choices=["mock", "fake", "tobii"], default="mock", help="mock = nothing real, fake = real worker with a pretend tracker, tobii = the real hardware")
    parser.add_argument("--data-dir", default=None, help="where recordings are saved (default: the recordings folder next to this one)")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--files-port", type=int, default=8767, help="where the website plays the videos from")
    parser.add_argument("--speed", type=float, default=1.0, help="mock and fake only: 10 = everything 10 times faster")
    parser.add_argument("--origin", action="append", default=[], help="allow another page, e.g. https://my-site.netlify.app")
    args = parser.parse_args()

    if args.mode == "mock":
        backend = MockBackend(speed=args.speed)
    else:
        from worker_backend import WorkerBackend  # only needed when a worker process is started

        data_dir = args.data_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "recordings")
        data_dir = os.path.abspath(data_dir)
        backend = WorkerBackend(args.mode, data_dir, speed=args.speed, files_port=args.files_port)
    if args.mode != "mock":
        import file_server

        file_server.start(data_dir, args.files_port)  # the website plays the videos from here
        print(f"Videos are served at http://{file_server.HOST}:{args.files_port}/")
    try:
        asyncio.run(serve(backend, args.port, DEFAULT_ORIGINS + args.origin))
    except KeyboardInterrupt:
        print("\nBridge stopped")


if __name__ == "__main__":
    main()
