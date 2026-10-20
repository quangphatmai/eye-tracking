import csv
import json
import numpy as np
import cv2
import sys

sys.path.append("PyGazeAnalyser")
from pygazeanalyser.detectors import fixation_detection

# ================= CẤU HÌNH =================
PARTICIPANT_DIR = "participant_16"   # đổi đúng folder participant mới nhất

GAZE_CSV = f"{PARTICIPANT_DIR}/gaze_data.csv"
FRAME_LOG = f"{PARTICIPANT_DIR}/frame_log.csv"
SCROLL_LOG = f"{PARTICIPANT_DIR}/scroll_log.csv"
NAV_LOG = f"{PARTICIPANT_DIR}/nav_log.csv"
VIDEO_IN = f"{PARTICIPANT_DIR}/session_recording.mp4"
VIDEO_OUT_DOT = f"{PARTICIPANT_DIR}/session_recording_with_gaze.mp4"
VIDEO_OUT_HEATMAP = f"{PARTICIPANT_DIR}/session_recording_with_heatmap.mp4"
SCREEN_INFO = f"{PARTICIPANT_DIR}/screen_info.json"

DOT_RADIUS = 14
FIXATION_RADIUS = 26

SCROLL_DPI_SCALE = 1.25   # đổi đúng theo % scaling Windows của bạn: 100%→1.0, 125%→1.25, 150%→1.5...

# ---- Tham số video scanpath tích lũy ----
SCANPATH_BASE_RADIUS = 10
SCANPATH_RADIUS_PER_SEC = 30
SCANPATH_COLOR = (255, 0, 255)
SCANPATH_LABEL_COLOR = (255, 255, 255)

# ---- Tham số video heatmap tích lũy ----
HEATMAP_RADIUS = 45         # bán kính (px) đốm nhiệt thêm vào mỗi frame tại gaze hiện tại
HEATMAP_BLUR_KSIZE = 65     # kernel Gaussian blur làm mềm đốm nhiệt — phải là số LẺ
HEATMAP_GAIN = 1.0          # mỗi frame cộng thêm bao nhiêu nhiệt — tăng để heatmap "nóng" nhanh hơn
HEATMAP_CAP = 6.0           # mức nhiệt coi là "max" khi tô màu — vì giờ KHÔNG decay, để cao hơn bản cũ (4.0) cho đỡ bão hoà sớm
HEATMAP_ALPHA_MAX = 0.75    # độ che màu heatmap tối đa lên frame gốc (0=trong suốt, 1=che hết)

with open(SCREEN_INFO) as f:
    info = json.load(f)
SCREEN_W, SCREEN_H = info["SCREEN_W"], info["SCREEN_H"]

# ---- Đọc gaze_data.csv ----
t_list, lx_list, ly_list, lv_list, rx_list, ry_list, rv_list = [], [], [], [], [], [], []
with open(GAZE_CSV) as f:
    for row in csv.DictReader(f):
        t_list.append(float(row["elapsed_sec"]))
        lx_list.append(float(row["left_x"]))
        ly_list.append(float(row["left_y"]))
        lv_list.append(int(row["left_valid"]))
        rx_list.append(float(row["right_x"]))
        ry_list.append(float(row["right_y"]))
        rv_list.append(int(row["right_valid"]))

t = np.array(t_list)
lx, ly, lv = np.array(lx_list), np.array(ly_list), np.array(lv_list)
rx, ry, rv = np.array(rx_list), np.array(ry_list), np.array(rv_list)
print(f"Read {len(t)} gaze samples, total {t[-1]:.1f}s.")

left_valid = lv == 1
right_valid = rv == 1

x_norm = np.where(left_valid & right_valid, (lx + rx) / 2,
          np.where(left_valid, lx,
          np.where(right_valid, rx, np.nan)))
y_norm = np.where(left_valid & right_valid, (ly + ry) / 2,
          np.where(left_valid, ly,
          np.where(right_valid, ry, np.nan)))

x = x_norm * SCREEN_W
y = y_norm * SCREEN_H

valid = ~(np.isnan(x) | np.isnan(y))
t_valid, x_valid, y_valid = t[valid], x[valid], y[valid]
time_valid_ms = t_valid * 1000.0

Sfix, Efix = fixation_detection(x_valid, y_valid, time_valid_ms, maxdist=25, mindur=50)
print(f"Detected {len(Efix)} fixations.")

# ---- Đọc frame_log.csv ----
frame_times = []
with open(FRAME_LOG) as f:
    for row in csv.DictReader(f):
        frame_times.append(float(row["elapsed_sec"]))
frame_times = np.array(frame_times)
print(f"Read {len(frame_times)} frame timestamps.")

# ---- Đọc scroll_log.csv ----
scroll_t_list, scroll_y_list = [], []
with open(SCROLL_LOG) as f:
    for row in csv.DictReader(f):
        scroll_t_list.append(float(row["elapsed_sec"]))
        scroll_y_list.append(float(row["scroll_y"]))
scroll_t = np.array(scroll_t_list)
scroll_y_arr = np.array(scroll_y_list)
scroll_y_arr = scroll_y_arr * SCROLL_DPI_SCALE   # <<< THÊM dòng này — quy đổi CSS px sang pixel vật lý
print(f"Read {len(scroll_t)} scroll timestamps.")

def get_scroll_at(time_sec):
    if len(scroll_t) == 0:
        return 0.0
    return float(np.interp(time_sec, scroll_t, scroll_y_arr))

# ---- Đọc nav_log.csv ----
nav_t_list = []
with open(NAV_LOG) as f:
    for row in csv.DictReader(f):
        nav_t_list.append(float(row["elapsed_sec"]))
nav_times = np.array(nav_t_list)
print(f"Read {len(nav_times)} navigation events.")

def get_segment(time_sec):
    if len(nav_times) == 0:
        return 0
    return max(0, int(np.searchsorted(nav_times, time_sec, side="right")) - 1)

def segment_time_range(s):
    start = nav_times[s] if len(nav_times) > 0 else 0.0
    end = nav_times[s + 1] if (len(nav_times) > 0 and s + 1 < len(nav_times)) else float("inf")
    return start, end

num_segments = len(nav_times) if len(nav_times) > 0 else 1

# ---- Mở video gốc, chuẩn bị 2 video output ----
cap = cv2.VideoCapture(VIDEO_IN)
frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
src_fps = cap.get(cv2.CAP_PROP_FPS)

fourcc = cv2.VideoWriter_fourcc(*"mp4v")
out_dot = cv2.VideoWriter(VIDEO_OUT_DOT, fourcc, src_fps, (frame_w, frame_h))
out_heatmap = cv2.VideoWriter(VIDEO_OUT_HEATMAP, fourcc, src_fps, (frame_w, frame_h))

# ---- Canvas nhiệt riêng cho từng trang — đủ cao để chứa toàn bộ phạm vi đã cuộn của trang đó ----
segment_scroll_min = []
segment_scroll_max = []
for s in range(num_segments):
    start, end = segment_time_range(s)
    mask = (scroll_t >= start) & (scroll_t < end)
    if np.any(mask):
        seg_vals = scroll_y_arr[mask]
        segment_scroll_min.append(float(seg_vals.min()))
        segment_scroll_max.append(float(seg_vals.max()))
    else:
        segment_scroll_min.append(0.0)
        segment_scroll_max.append(0.0)

heat_canvases = []
for s in range(num_segments):
    extra = segment_scroll_max[s] - segment_scroll_min[s]
    canvas_h = int(frame_h + extra) + 2
    heat_canvases.append(np.zeros((canvas_h, frame_w), dtype=np.float32))

# Fixation đã "xong" tính tới thời điểm hiện tại — tích lũy, kèm scroll offset + segment lúc ghi
drawn_fixations = []
added_indices = set()

# "Con dấu" nhiệt mềm — tạo 1 LẦN duy nhất
stamp_size = HEATMAP_RADIUS * 2 + HEATMAP_BLUR_KSIZE
stamp = np.zeros((stamp_size, stamp_size), dtype=np.float32)
cv2.circle(stamp, (stamp_size // 2, stamp_size // 2), HEATMAP_RADIUS, 1.0, -1)
stamp = cv2.GaussianBlur(stamp, (HEATMAP_BLUR_KSIZE, HEATMAP_BLUR_KSIZE), 0)
stamp_half = stamp_size // 2

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

    # ===== 1. Video scanpath tích lũy (bù scroll + lọc theo trang) =====
    current_time_ms = frame_time * 1000.0
    for idx, ev in enumerate(Efix):
        start_ms, dur_ms = ev[0], ev[2]
        end_ms = start_ms + dur_ms
        if end_ms <= current_time_ms and idx not in added_indices:
            added_indices.add(idx)
            scroll_at_record = get_scroll_at(start_ms / 1000.0)
            fixation_segment = get_segment(start_ms / 1000.0)
            drawn_fixations.append((ev, scroll_at_record, fixation_segment))

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

    # ===== 2. Video heatmap tích lũy (bù scroll + lọc theo trang) =====
    canvas = heat_canvases[current_segment]
    min_scroll_seg = segment_scroll_min[current_segment]
    canvas_h = canvas.shape[0]

    if has_gaze_now and 0 <= gx < frame_w:
        page_y = gy + scroll_now                      # vị trí "tuyệt đối trên trang"
        center_row = int(page_y - min_scroll_seg)      # vị trí tương ứng trên canvas
        y0 = center_row - stamp_half
        y1 = y0 + stamp_size
        x0 = gx - stamp_half
        x1 = x0 + stamp_size
        sy0, sy1 = max(0, y0), min(canvas_h, y1)
        sx0, sx1 = max(0, x0), min(frame_w, x1)
        if sy1 > sy0 and sx1 > sx0:
            canvas[sy0:sy1, sx0:sx1] += (
                stamp[sy0 - y0 : stamp_size - (y1 - sy1), sx0 - x0 : stamp_size - (x1 - sx1)]
                * HEATMAP_GAIN
            )

    viewport_top = int(scroll_now - min_scroll_seg)
    viewport_top = max(0, min(viewport_top, canvas_h - frame_h))
    heat_view = canvas[viewport_top: viewport_top + frame_h, :]

    heat_norm = np.clip(heat_view / HEATMAP_CAP, 0.0, 1.0)
    heat_color = cv2.applyColorMap((heat_norm * 255).astype(np.uint8), cv2.COLORMAP_JET)

    alpha_map = (heat_norm * HEATMAP_ALPHA_MAX)[:, :, None]
    frame_heat = (frame.astype(np.float32) * (1 - alpha_map)
                  + heat_color.astype(np.float32) * alpha_map).astype(np.uint8)

    out_heatmap.write(frame_heat)

    frame_idx += 1

cap.release()
out_dot.release()
out_heatmap.release()
print(f"Done. Scanpath video: {VIDEO_OUT_DOT}")
print(f"Done. Accumulated heatmap video: {VIDEO_OUT_HEATMAP}")