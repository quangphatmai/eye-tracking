"""
The FAST version of process_task.py: the same two videos (heatmap.mp4, scanpath.mp4), made quickly, so the observer can
watch them right after the task (the retrospective think-aloud) instead of waiting minutes.

process_task.py is not changed (it keeps the full-size version). What is different here:
  - the output is smaller: at most OUT_WIDTH pixels wide (1280 = 720p). Everything is drawn at that size, so it is all cheaper
  - the heat is still ADDED UP in float32 (the numbers are exactly the same as before); only the blending onto the picture is
    done on whole numbers (8 bit) and only on the rows that really have heat. Looks the same, costs about 10 times less
  - the scanpath layer is drawn once and re-used while nothing in it changes (no scroll, no new fixation)
  - the video is read in a second thread while the previous frame is drawn
  - H.264 with the "ultrafast" preset, both videos encoded at the same time by two ffmpeg processes

The logic of segments (a new page = a new heat canvas) and of the scroll is the same as in process_task.py.

Run by the bridge:   python process_task_fast.py <task folder>
Same files in, same files out, same JSON lines on stdout as process_task.py.
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading

import cv2
import numpy as np

import process_task as base
from process_task import (
    DEFAULT_SCALE_PERCENT, HEATMAP_ALPHA_MAX, HEATMAP_BLUR_KSIZE, HEATMAP_CAP, HEATMAP_GAIN, HEATMAP_OUT, HEATMAP_RADIUS,
    SCANPATH_BASE_RADIUS, SCANPATH_COLOR, SCANPATH_LABEL_COLOR, SCANPATH_OUT, SCANPATH_RADIUS_PER_SEC, VIDEO_IN,
    ProcessingError, emit, find_ffmpeg, lower_priority, read_csv,
)

OUT_WIDTH = 1280  # the videos are at most this wide (1280x720 for a 16:9 screen); smaller sources stay as they are
CRF = "27"  # a little lower quality than process_task.py (23): the file is smaller and encodes faster


class FastWriter:
    """Frames in, H.264 mp4 out (ffmpeg, preset ultrafast)."""

    def __init__(self, path, fps, width, height):
        command = [
            find_ffmpeg(), "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", f"{fps}", "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", CRF, "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            path,
        ]
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, frame):
        try:
            self.proc.stdin.write(memoryview(np.ascontiguousarray(frame)))  # no extra copy of the picture
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


def even(value):
    return max(2, int(round(value / 2.0)) * 2)


def process_task(task_dir, on_progress=lambda percent: None):
    path = lambda name: os.path.join(task_dir, name)
    video_in = path(VIDEO_IN)
    if not os.path.exists(video_in):
        raise ProcessingError(f"No screen recording in {task_dir}")

    sys.path.insert(0, os.path.abspath(base.PYGAZE_DIR))
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
    x, y = x_norm * screen_w, y_norm * screen_h  # screen pixels (full size)

    valid = ~(np.isnan(x) | np.isnan(y))
    t_valid, x_valid, y_valid = t[valid], x[valid], y[valid]
    if len(t_valid) == 0:
        raise ProcessingError("No valid gaze in this task (the eyes were never found)")

    # the fixations are found at full size (the same numbers as process_task.py), drawing is scaled later
    _, Efix = fixation_detection(x_valid, y_valid, t_valid * 1000.0, maxdist=25, mindur=50)

    frame_times = read_csv(path("frame_log.csv"), {"elapsed_sec": float})["elapsed_sec"]
    if len(frame_times) == 0:
        raise ProcessingError("frame_log.csv has no frames")

    scroll = read_csv(path("scroll_log.csv"), {"elapsed_sec": float, "scroll_y": float})
    scroll_t = scroll["elapsed_sec"]
    scroll_y_arr = scroll["scroll_y"] * scroll_dpi_scale  # screen pixels (full size)
    nav_times = read_csv(path("nav_log.csv"), {"elapsed_sec": float})["elapsed_sec"]

    def get_segment(time_sec):
        if len(nav_times) == 0:
            return 0
        return max(0, int(np.searchsorted(nav_times, time_sec, side="right")) - 1)

    def get_scroll_at(time_sec):
        # only the samples of the same page count: never glide from the old page's scroll to the new page's
        if len(scroll_t) == 0:
            return 0.0
        if len(nav_times) > 0:
            s = get_segment(time_sec)
            lo = int(np.searchsorted(scroll_t, nav_times[s], side="left"))
            hi = int(np.searchsorted(scroll_t, nav_times[s + 1], side="left")) if s + 1 < len(nav_times) else len(scroll_t)
            if hi <= lo:
                return 0.0
            return float(np.interp(time_sec, scroll_t[lo:hi], scroll_y_arr[lo:hi]))
        return float(np.interp(time_sec, scroll_t, scroll_y_arr))

    def segment_time_range(s):
        start = nav_times[s] if len(nav_times) > 0 else 0.0
        end = nav_times[s + 1] if (len(nav_times) > 0 and s + 1 < len(nav_times)) else float("inf")
        return start, end

    num_segments = len(nav_times) if len(nav_times) > 0 else 1

    cap = cv2.VideoCapture(video_in)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 15
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or len(frame_times)
    if src_w == 0 or src_h == 0:
        raise ProcessingError("The screen recording cannot be read")

    # the output size and the factor between the full-size numbers (gaze, scroll, fixations) and the output picture
    factor = min(1.0, OUT_WIDTH / float(src_w))
    frame_w, frame_h = (src_w, src_h) if factor == 1.0 else (even(src_w * factor), even(src_h * factor))
    factor_x, factor_y = frame_w / float(src_w), frame_h / float(src_h)
    factor = factor_x  # one factor for scroll and radii (the two are the same up to rounding)

    out_scanpath_tmp, out_heatmap_tmp = path("scanpath.tmp.mp4"), path("heatmap.tmp.mp4")
    out_dot = out_heatmap = None

    # the reader: decodes and shrinks the next frames while this thread draws the current one
    frames = queue.Queue(maxsize=12)
    stop_reading = threading.Event()

    def reader():
        try:
            while not stop_reading.is_set():
                ok, picture = cap.read()
                if not ok:
                    break
                if (frame_w, frame_h) != (src_w, src_h):
                    picture = cv2.resize(picture, (frame_w, frame_h), interpolation=cv2.INTER_LINEAR)
                while not stop_reading.is_set():
                    try:
                        frames.put(picture, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        finally:
            while not stop_reading.is_set():
                try:
                    frames.put(None, timeout=0.2)
                    break
                except queue.Full:
                    continue

    try:
        out_dot = FastWriter(out_scanpath_tmp, src_fps, frame_w, frame_h)
        out_heatmap = FastWriter(out_heatmap_tmp, src_fps, frame_w, frame_h)

        # the heat canvases (output pixels). Page height of each segment from its scroll range
        segment_scroll_min, segment_scroll_max = [], []
        for s in range(num_segments):
            start, end = segment_time_range(s)
            mask = (scroll_t >= start) & (scroll_t < end)
            if np.any(mask):
                values = scroll_y_arr[mask] * factor
                segment_scroll_min.append(float(values.min()))
                segment_scroll_max.append(float(values.max()))
            else:
                segment_scroll_min.append(0.0)
                segment_scroll_max.append(0.0)

        heat_canvases = []
        heat_rows = []  # [first, last+1] row of the canvas that ever got heat (nothing to blend outside of it)
        for s in range(num_segments):
            extra = segment_scroll_max[s] - segment_scroll_min[s]
            heat_canvases.append(np.zeros((int(frame_h + extra) + 2, frame_w), dtype=np.float32))
            heat_rows.append([None, None])

        drawn_fixations = [[] for _ in range(num_segments)]  # per page: (event, scroll at the time)
        next_fixation = 0

        radius_heat = max(2, int(round(HEATMAP_RADIUS * factor)))
        blur = max(3, int(round(HEATMAP_BLUR_KSIZE * factor)))
        blur += 1 - blur % 2  # odd
        stamp_size = radius_heat * 2 + blur
        stamp = np.zeros((stamp_size, stamp_size), dtype=np.float32)
        cv2.circle(stamp, (stamp_size // 2, stamp_size // 2), radius_heat, 1.0, -1)
        stamp = cv2.GaussianBlur(stamp, (blur, blur), 0)
        stamp *= HEATMAP_GAIN
        stamp_half = stamp_size // 2
        norm_scale = 255.0 / HEATMAP_CAP
        alpha_lut = (np.arange(256) * HEATMAP_ALPHA_MAX).astype(np.uint8)  # heat level 0..255 -> opacity 0..191

        label_scale = max(0.35, 0.5 * factor)  # the numbers in the circles shrink with the picture
        # the scanpath layer is only redrawn when it changes
        layer = layer_mask = None
        layer_has_shapes = False
        layer_key = None

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()

        last_percent = -1
        frame_idx = 0
        while True:
            frame = frames.get()
            if frame is None:
                break

            frame_time = frame_times[frame_idx] if frame_idx < len(frame_times) else frame_times[-1]
            has_gaze_now = t_valid[0] <= frame_time <= t_valid[-1]
            if has_gaze_now:
                gx = int(np.interp(frame_time, t_valid, x_valid) * factor_x)
                gy = int(np.interp(frame_time, t_valid, y_valid) * factor_y)

            current_segment = get_segment(frame_time)
            scroll_full = get_scroll_at(frame_time)
            scroll_now = scroll_full * factor  # output pixels

            # 1. scanpath: fixations that are "done" (always the first ones, the list is in time order)
            current_time_ms = frame_time * 1000.0
            while next_fixation < len(Efix) and Efix[next_fixation][0] + Efix[next_fixation][2] <= current_time_ms:
                ev = Efix[next_fixation]
                drawn_fixations[get_segment(ev[0] / 1000.0)].append((ev, get_scroll_at(ev[0] / 1000.0) * factor))
                next_fixation += 1

            key = (current_segment, len(drawn_fixations[current_segment]), round(scroll_now, 1))
            if key != layer_key:
                layer = np.zeros_like(frame)
                order = 0
                prev_center = None
                for ev, scroll_at_record in drawn_fixations[current_segment]:
                    order += 1
                    dur_ms, fx, fy = ev[2], ev[3] * factor_x, ev[4] * factor_y
                    adjusted_y = fy + (scroll_at_record - scroll_now)
                    if adjusted_y < -50 or adjusted_y > frame_h + 50:
                        prev_center = None
                        continue
                    center = (int(fx), int(adjusted_y))
                    radius = max(2, int((SCANPATH_BASE_RADIUS + (dur_ms / 1000.0) * SCANPATH_RADIUS_PER_SEC) * factor))
                    if prev_center is not None:
                        cv2.line(layer, prev_center, center, SCANPATH_COLOR, 2)
                    cv2.circle(layer, center, radius, SCANPATH_COLOR, -1)
                    cv2.circle(layer, center, radius, (255, 255, 255), 1)
                    cv2.putText(layer, str(order), (center[0] - int(8 * factor), center[1] + int(5 * factor)),
                                cv2.FONT_HERSHEY_SIMPLEX, label_scale, SCANPATH_LABEL_COLOR, 1, cv2.LINE_AA)
                    prev_center = center
                layer_mask = cv2.cvtColor(layer, cv2.COLOR_BGR2GRAY) > 0  # every drawn colour has some light in it
                layer_has_shapes = cv2.countNonZero(layer_mask.view(np.uint8)) > 0
                layer_key = key
            frame_dot = frame.copy()
            if layer_has_shapes:
                frame_dot[layer_mask] = layer[layer_mask]
            out_dot.write(frame_dot)

            # 2. heatmap: add the gaze point to this page's canvas (float32, exact), then blend what is visible
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
                    canvas[sy0:sy1, sx0:sx1] += stamp[sy0 - y0 : stamp_size - (y1 - sy1), sx0 - x0 : stamp_size - (x1 - sx1)]
                    rows = heat_rows[current_segment]
                    rows[0] = sy0 if rows[0] is None else min(rows[0], sy0)
                    rows[1] = sy1 if rows[1] is None else max(rows[1], sy1)

            frame_heat = frame.copy()
            rows = heat_rows[current_segment]
            if rows[0] is not None:
                viewport_top = max(0, min(int(scroll_now - min_scroll_seg), canvas_h - frame_h))
                a = max(rows[0], viewport_top) - viewport_top  # rows of the picture that can have heat
                b = min(rows[1], viewport_top + frame_h) - viewport_top
                if b > a:
                    part = canvas[viewport_top + a : viewport_top + b]
                    level = np.minimum(part * norm_scale, 255.0).astype(np.uint8)  # the same 0..255 as process_task.py
                    if cv2.countNonZero(level):
                        color = cv2.applyColorMap(level, cv2.COLORMAP_JET)
                        alpha = cv2.LUT(level, alpha_lut)  # 0..191: heat/cap * 0.75
                        alpha3 = cv2.merge([alpha, alpha, alpha])
                        keep = cv2.subtract(255, alpha3)
                        picture = cv2.multiply(frame[a:b], keep, scale=1 / 255.0)
                        frame_heat[a:b] = cv2.add(picture, cv2.multiply(color, alpha3, scale=1 / 255.0))
            out_heatmap.write(frame_heat)

            frame_idx += 1
            percent = min(99, int(frame_idx * 100 / max(1, total_frames)))
            if percent != last_percent:
                last_percent = percent
                on_progress(percent)

        stop_reading.set()
        thread.join()
        cap.release()
        if frame_idx == 0:
            raise ProcessingError("The screen recording has no frames")
        out_dot.close()
        out_heatmap.close()
        out_dot = out_heatmap = None
        os.replace(out_scanpath_tmp, path(SCANPATH_OUT))
        os.replace(out_heatmap_tmp, path(HEATMAP_OUT))
    except BaseException:
        stop_reading.set()
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
