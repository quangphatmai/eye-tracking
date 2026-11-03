"""
The bridge's side of the worker: starts worker.py as a separate program and talks to it (see worker.py for the lines).
Used by bridge_server.py for --mode fake and --mode tobii. Same methods as MockBackend.
"""

import asyncio
import json
import os
import sys

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")
PROCESS_TASK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "process_task.py")
START_TIMEOUT = 120  # seconds: Chrome and the eye tracker can take a while to start
QUIT_TIMEOUT = 20  # seconds the worker gets to save its files and close Chrome before it is killed


PASS_ON = ("familiarization_started", "intro_shown", "task_ready", "task_started")  # events that go on to the website


class WorkerError(Exception):
    pass


class WorkerBackend:
    tracker = None

    def __init__(self, hardware, data_dir, speed=1.0, files_port=8767):
        self.hardware = hardware  # "fake" or "tobii"
        self.name = hardware
        self.data_dir = data_dir
        self.speed = speed
        self.files_port = files_port  # file_server.py: where the website plays the videos from
        self.proc = None
        self.events = None  # lines from the worker, as dicts
        self.reader = None
        self.session_dir = None

    # -- talking to the worker --

    async def _read_events(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    self.events.put_nowait(json.loads(line))
                except ValueError:
                    print("worker said:", line.decode(errors="replace").rstrip())
        finally:
            self.events.put_nowait({"event": "exit"})

    async def _send(self, command):
        if self.proc is None or self.proc.returncode is not None:
            raise WorkerError("The hardware worker is not running")
        self.proc.stdin.write((json.dumps(command) + "\n").encode())
        await self.proc.stdin.drain()

    async def _next(self, timeout=None):
        """The next event from the worker. A failure of the worker becomes an exception."""
        event = await asyncio.wait_for(self.events.get(), timeout)
        if event["event"] in ("error", "fatal"):
            raise WorkerError(event.get("message", "The hardware worker failed"))
        if event["event"] == "exit":
            raise WorkerError("The hardware worker stopped unexpectedly (see the bridge terminal for its error)")
        return event

    # -- the same methods as MockBackend --

    async def open_session(self, session):
        self.session_dir = os.path.join(
            self.data_dir, f"study_{session['studyId']}", f"participant_{session['participantId']}_{session['id']}"
        )
        os.makedirs(self.session_dir, exist_ok=True)

        # The worker runs with this same Python (the venv that has Tobii, PsychoPy...), inside the session folder,
        # so files that Titta writes into the current folder end up there.
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", WORKER,
            "--hardware", self.hardware, "--dir", self.session_dir, "--speed", str(self.speed),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, cwd=self.session_dir,
        )
        self.events = asyncio.Queue()
        self.reader = asyncio.create_task(self._read_events())
        try:
            while True:
                event = await self._next(START_TIMEOUT)
                if event["event"] == "ready":
                    self.tracker = event.get("tracker")
                    return
        except asyncio.TimeoutError:
            await self.close_session()
            raise WorkerError("The eye tracker did not start in time. Is it plugged in?")
        except Exception:
            await self.close_session()
            raise

    async def calibrate(self, on_progress):
        await self._send({"cmd": "calibrate"})
        while True:
            event = await self._next()
            if event["event"] == "calibration_progress":
                await on_progress(event["point"], event["total"])
            elif event["event"] == "calibration_done":
                return {"quality": event.get("quality"), "avgErrorDeg": event.get("avgErrorDeg")}

    async def _run_command(self, command, stop, emit, done_event):
        """Sends a command, passes the worker's progress events on to the website until `done_event` arrives.
        The observer's skip (stop is set by the bridge) is passed on to the worker at once."""
        await self._send(command)

        async def pass_stop_on():
            while True:
                await stop.wait()
                await self._send({"cmd": "stop"})
                while stop.is_set():  # a skip is used up when the bridge/backend clears it: only then listen again
                    await asyncio.sleep(0.05)

        watcher = asyncio.create_task(pass_stop_on())
        try:
            while True:
                event = await self._next()
                name = event["event"]
                if name == done_event:
                    return event
                if name in PASS_ON:
                    data = {key: value for key, value in event.items() if key != "event"}
                    await emit(name, **data)
        finally:
            watcher.cancel()

    async def familiarize(self, url, duration, stop, emit):
        await self._run_command({"cmd": "familiarize", "url": url, "duration": duration}, stop, emit, "familiarization_done")
        # (the bridge sends "familiarization_done" to the website itself)

    async def run_task(self, index, url, text, duration, stop, emit):
        task_dir = os.path.join(self.session_dir, f"task_{index + 1}")
        command = {"cmd": "run_task", "index": index, "url": url, "text": text, "duration": duration, "dir": task_dir}
        event = await self._run_command(command, stop, emit, "task_finished")
        return event["reason"]

    async def process_participant(self, study_id, participant_id, session_id, task_count, on_progress):
        """Makes the heatmap and scanpath video of every task of one recorded session (process_task.py, one task at a time).
        A task that already has both videos is skipped, so pressing "Process videos" again continues where it stopped."""
        session_dir = os.path.join(self.data_dir, f"study_{study_id}", f"participant_{participant_id}_{session_id}")
        if not os.path.isdir(session_dir):
            raise WorkerError(f"The recording folder was not found: {session_dir}")

        videos = []
        made_any = False
        for index in range(task_count):
            task_name = f"task_{index + 1}"
            task_dir = os.path.join(session_dir, task_name)
            recording = os.path.join(task_dir, "session_recording.mp4")
            heatmap = os.path.join(task_dir, "heatmap.mp4")
            scanpath = os.path.join(task_dir, "scanpath.mp4")
            url = f"http://127.0.0.1:{self.files_port}/study_{study_id}/participant_{participant_id}_{session_id}/{task_name}"

            if self.hardware == "fake":  # the pretend tracker records no video: pretend to process
                for percent in (0, 50, 100):
                    await asyncio.sleep(0.2 / self.speed)
                    await on_progress(index, task_count, percent)
                videos.append({"task": index, "heatmap": None, "scanpath": None})
                made_any = True
                continue

            if not os.path.exists(recording):  # the task was skipped or never recorded
                await on_progress(index, task_count, 100)
                videos.append({"task": index, "heatmap": None, "scanpath": None})
                continue

            made_any = True
            done = (
                os.path.exists(heatmap)
                and os.path.exists(scanpath)
                and min(os.path.getmtime(heatmap), os.path.getmtime(scanpath)) >= os.path.getmtime(recording)
            )
            if not done:
                await self._process_task(task_dir, lambda percent, index=index: on_progress(index, task_count, percent))
            await on_progress(index, task_count, 100)
            videos.append({"task": index, "heatmap": f"{url}/heatmap.mp4", "scanpath": f"{url}/scanpath.mp4"})

        if not made_any:
            raise WorkerError("No recorded task was found for this participant")
        return videos

    async def _process_task(self, task_dir, on_percent):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", PROCESS_TASK, task_dir,
            stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event["event"] == "progress":
                    await on_percent(event["percent"])
                elif event["event"] == "error":
                    raise WorkerError(f"{os.path.basename(task_dir)}: {event.get('message', 'processing failed')}")
            code = await proc.wait()
            if code != 0:
                raise WorkerError(f"{os.path.basename(task_dir)}: the video program stopped (code {code}). See the bridge terminal")
        finally:
            if proc.returncode is None:  # cancelled (the website left) or failed: do not leave ffmpeg running
                proc.kill()
                await proc.wait()
            for leftover in ("heatmap.tmp.mp4", "scanpath.tmp.mp4"):  # half-made videos
                try:
                    os.remove(os.path.join(task_dir, leftover))
                except OSError:
                    pass

    async def close_session(self):
        """Stop the worker: end a running task (its data is saved), then quit. Kill it if it does not leave."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.returncode is None:
                for command in ({"cmd": "stop"}, {"cmd": "quit"}):
                    proc.stdin.write((json.dumps(command) + "\n").encode())
                await proc.stdin.drain()
                await asyncio.wait_for(proc.wait(), QUIT_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            proc.kill()
            await proc.wait()
        finally:
            if self.reader:
                self.reader.cancel()
