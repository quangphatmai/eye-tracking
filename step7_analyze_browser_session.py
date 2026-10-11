import csv
import json
import numpy as np
import cv2
import sys

sys.path.append("PyGazeAnalyser")
from pygazeanalyser.detectors import fixation_detection

# ================= CẤU HÌNH =================
PARTICIPANT_DIR = "participant_07"   # đổi đúng folder participant mới nhất

GAZE_CSV = f"{PARTICIPANT_DIR}/gaze_data.csv"
FRAME_LOG = f"{PARTICIPANT_DIR}/frame_log.csv"
VIDEO_IN = f"{PARTICIPANT_DIR}/session_recording.mp4"
VIDEO_OUT_DOT = f"{PARTICIPANT_DIR}/session_recording_with_gaze.mp4"
VIDEO_OUT_HEATMAP = f"{PARTICIPANT_DIR}/session_recording_with_heatmap.mp4"
SCREEN_INFO = f"{PARTICIPANT_DIR}/screen_info.json"

TRAIL_SECONDS = 0.35
TRAIL_STEPS = 8
DOT_RADIUS = 14
FIXATION_RADIUS = 26

# ---- Tham số video heatmap động (MỚI) ----
HEATMAP_DECAY = 0.92        # mỗi frame nhiệt cũ còn lại 92% — giảm (vd 0.85) để vệt tắt nhanh hơn, tăng (0.96) để tồn lâu hơn
HEATMAP_RADIUS = 45         # bán kính (px) đốm nhiệt thêm vào mỗi frame tại gaze hiện tại
HEATMAP_BLUR_KSIZE = 65     # kernel Gaussian blur làm mềm đốm nhiệt — phải là số LẺ
HEATMAP_GAIN = 1.0          # mỗi frame cộng thêm bao nhiêu nhiệt — tăng để heatmap "nóng" nhanh hơn
HEATMAP_CAP = 4.0           # mức nhiệt coi là "max" khi tô màu — hạ xuống nếu muốn mau chuyển đỏ
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
print(f"Đọc được {len(t)} mẫu gaze, tổng {t[-1]:.1f}s.")

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
print(f"Phát hiện {len(Efix)} fixations.")

fixation_windows = [(ev[0] / 1000.0, (ev[0] + ev[2]) / 1000.0) for ev in Efix]

def is_in_fixation(time_sec):
    for start, end in fixation_windows:
        if start <= time_sec <= end:
            return True
    return False

# ---- Đọc frame_log.csv ----
frame_times = []
with open(FRAME_LOG) as f:
    for row in csv.DictReader(f):
        frame_times.append(float(row["elapsed_sec"]))
frame_times = np.array(frame_times)
print(f"Đọc được {len(frame_times)} frame timestamps.")

# ---- Mở video gốc, chuẩn bị 2 video output ----
cap = cv2.VideoCapture(VIDEO_IN)
frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
src_fps = cap.get(cv2.CAP_PROP_FPS)

fourcc = cv2.VideoWriter_fourcc(*"mp4v")
out_dot = cv2.VideoWriter(VIDEO_OUT_DOT, fourcc, src_fps, (frame_w, frame_h))
out_heatmap = cv2.VideoWriter(VIDEO_OUT_HEATMAP, fourcc, src_fps, (frame_w, frame_h))

# Bản đồ nhiệt tích lũy — tồn tại xuyên suốt TOÀN BỘ video, không reset giữa frame
heat_accum = np.zeros((frame_h, frame_w), dtype=np.float32)

# "Con dấu" nhiệt mềm — tạo 1 LẦN duy nhất ở đây, không tạo lại/blur lại mỗi frame
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

    # ===== 1. Video chấm + vệt mờ — bản tối ưu, chỉ xử lý vùng nhỏ quanh chấm =====
    frame_dot = frame.copy()
    for step in range(TRAIL_STEPS, 0, -1):
        sample_time = frame_time - (TRAIL_SECONDS * step / TRAIL_STEPS)
        if sample_time < t_valid[0] or sample_time > t_valid[-1]:
            continue
        sx = int(np.interp(sample_time, t_valid, x_valid))
        sy = int(np.interp(sample_time, t_valid, y_valid))
        alpha = 1.0 - (step / TRAIL_STEPS)
        radius = max(3, int(DOT_RADIUS * (0.4 + 0.6 * alpha)))

        x0, x1 = max(0, sx - radius - 2), min(frame_w, sx + radius + 2)
        y0, y1 = max(0, sy - radius - 2), min(frame_h, sy + radius + 2)
        if x1 <= x0 or y1 <= y0:
            continue
        roi = frame_dot[y0:y1, x0:x1]
        overlay_roi = roi.copy()
        cv2.circle(overlay_roi, (sx - x0, sy - y0), radius, (0, 255, 0), -1)
        frame_dot[y0:y1, x0:x1] = cv2.addWeighted(overlay_roi, alpha * 0.5, roi, 1 - alpha * 0.5, 0)

    if has_gaze_now:
        radius = FIXATION_RADIUS if is_in_fixation(frame_time) else DOT_RADIUS
        color = (0, 140, 255) if is_in_fixation(frame_time) else (0, 255, 0)
        cv2.circle(frame_dot, (gx, gy), radius, color, -1)
        cv2.circle(frame_dot, (gx, gy), radius, (255, 255, 255), 2)

    out_dot.write(frame_dot)

    # ===== 2. Video heatmap động — MỚI =====
    # (a) Làm mờ dần toàn bộ bản đồ nhiệt — vùng cũ tự nhạt đi nếu mắt rời khỏi đó
    heat_accum *= HEATMAP_DECAY

    # (b) Cộng thêm nhiệt mới tại gaze hiện tại — dùng lại "con dấu" đã tạo sẵn, không blur lại
    if has_gaze_now and 0 <= gx < frame_w and 0 <= gy < frame_h:
        y0 = gy - stamp_half
        y1 = y0 + stamp_size      # luôn đúng = stamp_size, không dùng gy + stamp_half nữa
        x0 = gx - stamp_half
        x1 = x0 + stamp_size
        sy0, sy1 = max(0, y0), min(frame_h, y1)
        sx0, sx1 = max(0, x0), min(frame_w, x1)
        if sy1 > sy0 and sx1 > sx0:
            heat_accum[sy0:sy1, sx0:sx1] += (
                stamp[sy0 - y0 : stamp_size - (y1 - sy1), sx0 - x0 : stamp_size - (x1 - sx1)]
                * HEATMAP_GAIN
            )

    # (c) Chuẩn hoá về 0..1 theo mức cap cố định (để màu không chớp giữa các frame)
    heat_norm = np.clip(heat_accum / HEATMAP_CAP, 0.0, 1.0)

    # (d) Áp colormap JET (xanh = mới/nhạt, đỏ = đang nhìn lâu/nóng)
    heat_color = cv2.applyColorMap((heat_norm * 255).astype(np.uint8), cv2.COLORMAP_JET)

    # (e) Alpha-blend theo từng pixel — chỗ không có nhiệt giữ nguyên frame gốc,
    #     chỉ "cháy" màu ở đúng chỗ có gaze, không làm tối cả video
    alpha_map = (heat_norm * HEATMAP_ALPHA_MAX)[:, :, None]
    frame_heat = (frame.astype(np.float32) * (1 - alpha_map)
                  + heat_color.astype(np.float32) * alpha_map).astype(np.uint8)

    out_heatmap.write(frame_heat)

    frame_idx += 1

cap.release()
out_dot.release()
out_heatmap.release()
print(f"Xong. Video chấm+vệt: {VIDEO_OUT_DOT}")
print(f"Xong. Video heatmap động: {VIDEO_OUT_HEATMAP}")