import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import glob
import shutil
import os
import re
import json
import time
import threading
import csv
import subprocess   # <<< MỚI
import sys          # <<< MỚI

import numpy as np
import cv2
import mss
import screeninfo   # <<< MỚI — để check số màn hình trước khi bật overlay

import tobii_research as tr
from titta import Titta
from psychopy import visual, core
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # <<< MỚI — để trỏ đúng đường dẫn 2 file step5/test_live_overlay dù chạy từ đâu

user32 = ctypes.windll.user32
SCREEN_W, SCREEN_H = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
print(f"Độ phân giải màn hình thật (vật lý): {SCREEN_W}x{SCREEN_H}")

TARGET_URL = "https://www.uvu.edu"
SESSION_SECONDS = 60
COUNTDOWN_SECONDS = 5
TOBII_MODEL = "Tobii Pro Nano"
RECORDING_FPS = 15

def get_next_participant_id(base_dir="."):
    existing = [d for d in os.listdir(base_dir) if re.match(r"^participant_\d+$", d)]
    numbers = [int(d.split("_")[1]) for d in existing]
    next_num = max(numbers, default=0) + 1
    return f"participant_{next_num:02d}"

output_dir = get_next_participant_id()
os.makedirs(output_dir)
print(f"Participant này: {output_dir}")

calib_dir = os.path.join(output_dir, "calibration_results")
os.makedirs(calib_dir, exist_ok=True)

# ================= 0. Mở Chrome trước =================
options = webdriver.ChromeOptions()
options.add_argument("--kiosk")
options.add_experimental_option("excludeSwitches", ["enable-automation"])
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_experimental_option("useAutomationExtension", False)

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
driver.get("about:blank")

# ================= 1. Calibration =================
settings = Titta.get_defaults(TOBII_MODEL)
settings.N_CAL_TARGETS = 9
tracker = Titta.Connect(settings)
tracker.init()

win = visual.Window(monitor="testMonitor", fullscr=True, units="pix", color=(0, 0, 0))
tracker.calibrate(win)

for pattern in ["*.cal", "*calibration_image*.png", "*validation_image*.png"]:
    for file in glob.glob(pattern):
        shutil.move(file, os.path.join(calib_dir, file))
print(f"Đã lưu kết quả calibration vào: {calib_dir}/")

# ================= 2. Màn hình đếm ngược =================
instruction_text = visual.TextStim(
    win,
    text="Task 1 is about to start.\n\nPlease look around freely\n",
    color="white", height=36, wrapWidth=1000, pos=(0, 150),
)
countdown_text = visual.TextStim(win, text="", color="white", height=90, pos=(0, -100))

clock = core.Clock()
for remaining in range(COUNTDOWN_SECONDS, 0, -1):
    clock.reset()
    while clock.getTime() < 1.0:
        instruction_text.draw()
        countdown_text.text = str(remaining)
        countdown_text.draw()
        win.flip()

win.close()

# ================= 2.5 Bật live overlay cho observer — CHỈ sau khi PsychoPy đã đóng hẳn =================  # <<< MỚI — cả block này
# Quan trọng: đặt NGAY SAU win.close() ở trên, TRƯỚC mục 3 bên dưới — lúc này PsychoPy không còn giữ GPU
# display pipeline nữa, nên test_live_overlay.py (dùng mss) không bị tranh chấp GPU với calibration nữa.
monitors = screeninfo.get_monitors()
if len(monitors) > 1:
    websocket_proc = subprocess.Popen([sys.executable, os.path.join(SCRIPT_DIR, "step5_websocket_server.py")])
    overlay_proc = subprocess.Popen([sys.executable, os.path.join(SCRIPT_DIR, "test_live_overlay.py")])
    time.sleep(2)  # cho 2 process kia kịp kết nối tracker + mở cửa sổ Tkinter trước khi vào task thật
else:
    websocket_proc = overlay_proc = None
    print("Chỉ phát hiện 1 màn hình — bỏ qua live overlay (không cần cho máy 1 màn hình).")
# <<< HẾT PHẦN MỚI 2.5

# ================= 3. Gaze logging — vẫn đồng hồ Python như cũ =================
found = tr.find_all_eyetrackers()
eyetracker = found[0]

gaze_log = []
session_start = None

def gaze_data_callback(gaze_data):
    t = time.time() - session_start
    lx, ly = gaze_data["left_gaze_point_on_display_area"]
    rx, ry = gaze_data["right_gaze_point_on_display_area"]
    lvalid = gaze_data["left_gaze_point_validity"]
    rvalid = gaze_data["right_gaze_point_validity"]
    gaze_log.append((t, lx, ly, lvalid, rx, ry, rvalid))

# ================= 4. Ghi màn hình song song (THAY cho scroll tracking) =================
video_path = os.path.join(output_dir, "session_recording.mp4")
frame_log = []
stop_flag = threading.Event()

def record_screen():
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(video_path, fourcc, RECORDING_FPS,
                                  (monitor["width"], monitor["height"]))
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

# ================= 5. Bắt đầu ghi =================
session_start = time.time()
eyetracker.subscribe_to(tr.EYETRACKER_GAZE_DATA, gaze_data_callback, as_dictionary=True)

record_thread = threading.Thread(target=record_screen)
record_thread.start()

print("=== BẮT ĐẦU GHI — đang mở trang ===")
driver.get(TARGET_URL)

time.sleep(SESSION_SECONDS)

# ================= 6. Dừng ghi =================
eyetracker.unsubscribe_from(tr.EYETRACKER_GAZE_DATA, gaze_data_callback)
stop_flag.set()
record_thread.join()

if overlay_proc:        # <<< MỚI
    overlay_proc.terminate()      # <<< MỚI
if websocket_proc:      # <<< MỚI
    websocket_proc.terminate()    # <<< MỚI

print(f"Đã ghi được {len(gaze_log)} mẫu gaze trong {gaze_log[-1][0]:.2f}s (đồng hồ Python)")
if frame_log:
    actual_fps = len(frame_log) / max(frame_log[-1], 0.001)
    print(f"Đã ghi được {len(frame_log)} frame video ({actual_fps:.1f} fps thực tế)")

with open(os.path.join(output_dir, "gaze_data.csv"), "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["elapsed_sec", "left_x", "left_y", "left_valid", "right_x", "right_y", "right_valid"])
    writer.writerows(gaze_log)

with open(os.path.join(output_dir, "frame_log.csv"), "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["frame_index", "elapsed_sec"])
    writer.writerows(enumerate(frame_log))

with open(os.path.join(output_dir, "screen_info.json"), "w") as f:
    json.dump({"SCREEN_W": SCREEN_W, "SCREEN_H": SCREEN_H}, f)

driver.quit()

print(f"Xong. Dữ liệu lưu trong folder: {output_dir}/")
print(f"  - gaze_data.csv ({len(gaze_log)} mẫu)")
print(f"  - session_recording.mp4 ({len(frame_log)} frames)")
print(f"  - frame_log.csv (timestamp từng frame)")