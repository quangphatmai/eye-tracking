"""
Makes the two videos of ONE task from its raw recording: heatmap.mp4 and scanpath.mp4.

This is step7_analyze_browser_session.py as a program the bridge can start (step7 itself is not changed).
What is different from step7:
  - the folder comes from the command line, not from a variable you edit
  - the Windows scaling comes from screen_info.json (SCALE_PERCENT), not from a typed-in 1.25
  - the videos are written as H.264 (through ffmpeg), so a browser can play them. step7 wrote mp4v, which browsers cannot play
  - it reports its progress as JSON lines on stdout

Run by the bridge:   python process_task.py <task folder>
By hand:             python process_task.py ..\\recordings\\study_1\\participant_16_ab12cd34\\task_1

The task folder must hold: gaze_data.csv, frame_log.csv, scroll_log.csv, nav_log.csv, screen_info.json, session_recording.mp4
It gets: heatmap.mp4, scanpath.mp4

stdout lines:  {"event": "progress", "percent": 40}
               {"event": "done", "heatmap": "heatmap.mp4", "scanpath": "scanpath.mp4"}
               {"event": "error", "message": "..."}
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys

import cv2
import numpy as np

# the same settings as step7
DEFAULT_SCALE_PERCENT = 125  # only for old recordings that have no SCALE_PERCENT (step7 had 1.25 typed in)

SCANPATH_BASE_RADIUS = 10
SCANPATH_RADIUS_PER_SEC = 30
SCANPATH_COLOR = (255, 0, 255)
SCANPATH_LABEL_COLOR = (255, 255, 255)

HEATMAP_RADIUS = 45
HEATMAP_BLUR_KSIZE = 65  # must be odd
HEATMAP_GAIN = 1.0
HEATMAP_CAP = 6.0
HEATMAP_ALPHA_MAX = 0.75

HERE = os.path.dirname(os.path.abspath(__file__))
PYGAZE_DIR = os.path.join(HERE, "..", "PyGazeAnalyser")  # the folder step7 adds to its path

VIDEO_IN = "session_recording.mp4"
HEATMAP_OUT = "heatmap.mp4"
SCANPATH_OUT = "scanpath.mp4"


class ProcessingError(Exception):
    pass


def emit(event, **data):
    sys.__stdout__.write(json.dumps({"event": event, **data}) + "\n")
    sys.__stdout__.flush()


# ---------- ffmpeg: frames in, H.264 mp4 out ----------


def find_ffmpeg():
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    bundled = os.path.join(sys.prefix, "share", "ffpyplayer", "ffmpeg", "bin", "ffmpeg.exe")  # the one active.txt uses
    if os.path.exists(bundled):
        return bundled
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise ProcessingError("ffmpeg was not found. Install it with: pip install imageio-ffmpeg")


class H264Writer:
    """cv2.VideoWriter look-alike: write(frame) sends the frame to ffmpeg, which makes an H.264 mp4 (plays in every browser)."""

    def __init__(self, path, fps, width, height):
        self.path = path
        command = [
            find_ffmpeg(), "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", f"{fps}", "-i", "-",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",  # yuv420p needs even sizes
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            path,
        ]
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, frame):
        try:
            self.proc.stdin.write(frame.tobytes())
        except (BrokenPipeError, OSError):
            raise ProcessingError("ffmpeg stopped: " + self.proc.stderr.read().decode(errors="replace")[-300:])

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        error = self.proc.stderr.read().decode(errors="replace")
        code = self.proc.wait()
        if code != 0:
            raise ProcessingError("ffmpeg failed: " + error[-300:])

    def abort(self):
        self.proc.kill()
        self.proc.wait()


# ---------- the analysis (step7) ----------


def read_csv(path, columns):
    if not os.path.exists(path):
        raise ProcessingError(f"Missing {os.path.basename(path)}")
    rows = {name: [] for name in columns}
    with open(path) as file:
        for row in csv.DictReader(file):
            for name, kind in columns.items():
                rows[name].append(kind(row[name]))
    return {name: np.array(values) for name, values in rows.items()}


def process_task(task_dir, on_progress=lambda percent: None):
    path = lambda name: os.path.join(task_dir, name)
    video_in = path(VIDEO_IN)
    if not os.path.exists(video_in):
        raise ProcessingError(f"No screen recording in {task_dir}")

    sys.path.insert(0, os.path.abspath(PYGAZE_DIR))
    from pygazeanalyser.detectors import fixation_detection

    with open(path("screen_info.json")) as file:
        info = json.load(file)
    screen_w, screen_h = info["SCREEN_W"], info["SCREEN_H"]
    scroll_dpi_scale = info.get("SCALE_PERCENT", DEFAULT_SCALE_PERCENT) / 100.0

    gaze = read_csv(path("gaze_data.csv"), {"elapsed_sec": float, "left_x": float, "left_y": float, "left_valid": int,
                                             "right_x": float, "right_y": float, "right_valid": int})
    t, lx, ly, lv = gaze["elapsed_sec"], gaze["left_x"], gaze["left_y"], gaze["left_valid"]
    rx, ry, rv = gaze["right_x"], gaze["right_y"], gaze["right_valid"]
    if len(t) == 0:
        raise ProcessingError("gaze_data.csv has no samples")

    left_valid, right_valid = lv == 1, rv == 1
    x_norm = np.where(left_valid & right_valid, (lx + rx) / 2, np.where(left_valid, lx, np.where(right_valid, rx, np.nan)))
    y_norm = np.where(left_valid & right_valid, (ly + ry) / 2, np.where(left_valid, ly, np.where(right_valid, ry, np.nan)))
    x, y = x_norm * screen_w, y_norm * screen_h

    valid = ~(np.isnan(x) | np.isnan(y))
    t_valid, x_valid, y_valid = t[valid], x[valid], y[valid]
    if len(t_valid) == 0:
        raise ProcessingError("No valid gaze in this task (the eyes were never found)")

    _, Efix = fixation_detection(x_valid, y_valid, t_valid * 1000.0, maxdist=25, mindur=50)

    frame_times = read_csv(path("frame_log.csv"), {"elapsed_sec": float})["elapsed_sec"]
    if len(frame_times) == 0:
        raise ProcessingError("frame_log.csv has no frames")

    scroll = read_csv(path("scroll_log.csv"), {"elapsed_sec": float, "scroll_y": float})
    scroll_t = scroll["elapsed_sec"]
    scroll_y_arr = scroll["scroll_y"] * scroll_dpi_scale  # CSS pixels -> screen pixels

    def get_scroll_at(time_sec):
        if len(scroll_t) == 0:
            return 0.0
        return float(np.interp(time_sec, scroll_t, scroll_y_arr))

    nav_times = read_csv(path("nav_log.csv"), {"elapsed_sec": float})["elapsed_sec"]

    def get_segment(time_sec):
        if len(nav_times) == 0:
            return 0
        return max(0, int(np.searchsorted(nav_times, time_sec, side="right")) - 1)

    def segment_time_range(s):
        start = nav_times[s] if len(nav_times) > 0 else 0.0
        end = nav_times[s + 1] if (len(nav_times) > 0 and s + 1 < len(nav_times)) else float("inf")
        return start, end

    num_segments = len(nav_times) if len(nav_times) > 0 else 1

    cap = cv2.VideoCapture(video_in)
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 15
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or len(frame_times)
    if frame_w == 0 or frame_h == 0:
        raise ProcessingError("The screen recording cannot be read")

    # written under a temporary name: a half-finished video never looks like a finished one
    out_scanpath_tmp, out_heatmap_tmp = path("scanpath.tmp.mp4"), path("heatmap.tmp.mp4")
    out_dot = out_heatmap = None
    try:
        out_dot = H264Writer(out_scanpath_tmp, src_fps, frame_w, frame_h)
        out_heatmap = H264Writer(out_heatmap_tmp, src_fps, frame_w, frame_h)

        segment_scroll_min, segment_scroll_max = [], []
        for s in range(num_segments):
            start, end = segment_time_range(s)
            mask = (scroll_t >= start) & (scroll_t < end)
            if np.any(mask):
                values = scroll_y_arr[mask]
                segment_scroll_min.append(float(values.min()))
                segment_scroll_max.append(float(values.max()))
            else:
                segment_scroll_min.append(0.0)
                segment_scroll_max.append(0.0)

        heat_canvases = []
        for s in range(num_segments):
            extra = segment_scroll_max[s] - segment_scroll_min[s]
            heat_canvases.append(np.zeros((int(frame_h + extra) + 2, frame_w), dtype=np.float32))

        drawn_fixations = []
        next_fixation = 0  # Efix is in time order: the fixations that are "done" are always the first ones

        stamp_size = HEATMAP_RADIUS * 2 + HEATMAP_BLUR_KSIZE
        stamp = np.zeros((stamp_size, stamp_size), dtype=np.float32)
        cv2.circle(stamp, (stamp_size // 2, stamp_size // 2), HEATMAP_RADIUS, 1.0, -1)
        stamp = cv2.GaussianBlur(stamp, (HEATMAP_BLUR_KSIZE, HEATMAP_BLUR_KSIZE), 0)
        stamp_half = stamp_size // 2

        last_percent = -1
        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            frame_time = frame_times[frame_idx] if frame_idx < len(frame_times) else frame_times[-1]
            has_gaze_now = t_valid[0] <= frame_time <= t_valid[-1]
            if has_gaze_now:
                gx = int(np.interp(frame_time, t_valid, x_valid))
                gy = int(np.interp(frame_time, t_valid, y_valid))

            current_segment = get_segment(frame_time)
            scroll_now = get_scroll_at(frame_time)

            # 1. scanpath video
            current_time_ms = frame_time * 1000.0
            while next_fixation < len(Efix) and Efix[next_fixation][0] + Efix[next_fixation][2] <= current_time_ms:
                ev = Efix[next_fixation]
                drawn_fixations.append((ev, get_scroll_at(ev[0] / 1000.0), get_segment(ev[0] / 1000.0)))
                next_fixation += 1

            frame_dot = frame.copy()
            order = 0
            prev_center = None
            for ev, scroll_at_record, fixation_segment in drawn_fixations:
                if fixation_segment != current_segment:
                    continue
                order += 1
                dur_ms, fx, fy = ev[2], ev[3], ev[4]
                adjusted_y = fy + (scroll_at_record - scroll_now)
                if adjusted_y < -50 or adjusted_y > frame_h + 50:
                    prev_center = None
                    continue
                center = (int(fx), int(adjusted_y))
                radius = int(SCANPATH_BASE_RADIUS + (dur_ms / 1000.0) * SCANPATH_RADIUS_PER_SEC)
                if prev_center is not None:
                    cv2.line(frame_dot, prev_center, center, SCANPATH_COLOR, 2)
                cv2.circle(frame_dot, center, radius, SCANPATH_COLOR, -1)
                cv2.circle(frame_dot, center, radius, (255, 255, 255), 1)
                cv2.putText(frame_dot, str(order), (center[0] - 8, center[1] + 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, SCANPATH_LABEL_COLOR, 1, cv2.LINE_AA)
                prev_center = center
            out_dot.write(frame_dot)

            # 2. heatmap video
            canvas = heat_canvases[current_segment]
            min_scroll_seg = segment_scroll_min[current_segment]
            canvas_h = canvas.shape[0]

            if has_gaze_now and 0 <= gx < frame_w:
                page_y = gy + scroll_now
                center_row = int(page_y - min_scroll_seg)
                y0 = center_row - stamp_half
                y1 = y0 + stamp_size
                x0 = gx - stamp_half
                x1 = x0 + stamp_size
                sy0, sy1 = max(0, y0), min(canvas_h, y1)
                sx0, sx1 = max(0, x0), min(frame_w, x1)
                if sy1 > sy0 and sx1 > sx0:
                    canvas[sy0:sy1, sx0:sx1] += (
                        stamp[sy0 - y0 : stamp_size - (y1 - sy1), sx0 - x0 : stamp_size - (x1 - sx1)] * HEATMAP_GAIN
                    )

            viewport_top = int(scroll_now - min_scroll_seg)
            viewport_top = max(0, min(viewport_top, canvas_h - frame_h))
            heat_view = canvas[viewport_top : viewport_top + frame_h, :]

            heat_norm = np.clip(heat_view / HEATMAP_CAP, 0.0, 1.0)
            heat_color = cv2.applyColorMap((heat_norm * 255).astype(np.uint8), cv2.COLORMAP_JET)
            alpha_map = (heat_norm * HEATMAP_ALPHA_MAX)[:, :, None]
            frame_heat = (frame.astype(np.float32) * (1 - alpha_map) + heat_color.astype(np.float32) * alpha_map).astype(np.uint8)
            out_heatmap.write(frame_heat)

            frame_idx += 1
            percent = min(99, int(frame_idx * 100 / max(1, total_frames)))
            if percent != last_percent:
                last_percent = percent
                on_progress(percent)

        cap.release()
        if frame_idx == 0:
            raise ProcessingError("The screen recording has no frames")
        out_dot.close()
        out_heatmap.close()
        out_dot = out_heatmap = None
        os.replace(out_scanpath_tmp, path(SCANPATH_OUT))
        os.replace(out_heatmap_tmp, path(HEATMAP_OUT))
    except BaseException:
        cap.release()
        for writer in (out_dot, out_heatmap):
            if writer:
                writer.abort()
        for leftover in (out_scanpath_tmp, out_heatmap_tmp):
            if os.path.exists(leftover):
                os.remove(leftover)
        raise
    on_progress(100)
    return {"heatmap": HEATMAP_OUT, "scanpath": SCANPATH_OUT}


def lower_priority():
    """Nothing else on this computer (the browser, the next participant) should feel this work."""
    try:
        import psutil

        process = psutil.Process()
        process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task_dir")
    args = parser.parse_args()

    sys.stdout = sys.stderr  # stray prints must not mix with the JSON lines (emit writes to the real stdout)
    lower_priority()
    try:
        result = process_task(args.task_dir, lambda percent: emit("progress", percent=percent))
        emit("done", **result)
        return 0
    except ProcessingError as problem:
        emit("error", message=str(problem))
    except Exception as problem:
        import traceback

        traceback.print_exc()
        emit("error", message=f"{type(problem).__name__}: {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
