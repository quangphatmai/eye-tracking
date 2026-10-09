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
import base64
import threading
import csv

import tobii_research as tr
from titta import Titta
from psychopy import visual, core
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

user32 = ctypes.windll.user32
SCREEN_W, SCREEN_H = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
print(f"Độ phân giải màn hình thật (vật lý): {SCREEN_W}x{SCREEN_H}")

TARGET_URL = "https://www.uvu.edu"
SESSION_SECONDS = 60
COUNTDOWN_SECONDS = 5
TOBII_MODEL = "Tobii Pro Nano"

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

# ================= 1. Calibration (vẫn dùng Titta — calibration áp dụng lên chính
#      thiết bị phần cứng, không phụ thuộc việc sau đó ai subscribe dữ liệu) =================
settings = Titta.get_defaults(TOBII_MODEL)
settings.N_CAL_TARGETS = 9
tracker = Titta.Connect(settings)
tracker.init()

win = visual.Window(monitor="testMonitor", fullscr=True, units="pix", color=(-1, -1, -1))
tracker.calibrate(win)

for pattern in ["*.cal", "*calibration_image*.png", "*validation_image*.png"]:
    for file in glob.glob(pattern):
        shutil.move(file, os.path.join(calib_dir, file))
print(f"Đã lưu kết quả calibration vào: {calib_dir}/")

# ================= 2. Màn hình đếm ngược =================
instruction_text = visual.TextStim(
    win,
    text="Task 1 sắp bắt đầu.\n\nKhi trang web hiện ra, hãy nhìn vào thanh điều hướng\n(nav bar) trên cùng trước tiên.",
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

# ================= 3. Lấy eyetracker thô (tobii_research) để ghi bằng đồng hồ Python,
#      KHÔNG qua tracker.save_data() nữa — tránh lệch đồng hồ thiết bị =================
found = tr.find_all_eyetrackers()
eyetracker = found[0]

gaze_log = []   # mỗi phần tử: (elapsed_sec, lx, ly, lvalid, rx, ry, rvalid)
session_start = None

def gaze_data_callback(gaze_data):
    t = time.time() - session_start
    lx, ly = gaze_data["left_gaze_point_on_display_area"]
    rx, ry = gaze_data["right_gaze_point_on_display_area"]
    lvalid = gaze_data["left_gaze_point_validity"]
    rvalid = gaze_data["right_gaze_point_validity"]
    gaze_log.append((t, lx, ly, lvalid, rx, ry, rvalid))

# ================= 4. Scroll log song song =================
scroll_log = []
stop_flag = threading.Event()

def log_scroll():
    while not stop_flag.is_set():
        try:
            y = driver.execute_script("return window.scrollY;")
            scroll_log.append((time.time() - session_start, y))
        except Exception:
            pass
        time.sleep(0.1)

# ================= 5. Bắt đầu ghi — TẤT CẢ cùng chung 1 đồng hồ Python =================
session_start = time.time()
eyetracker.subscribe_to(tr.EYETRACKER_GAZE_DATA, gaze_data_callback, as_dictionary=True)

scroll_thread = threading.Thread(target=log_scroll)
scroll_thread.start()

print("=== BẮT ĐẦU GHI — đang mở trang ===")
driver.get(TARGET_URL)

time.sleep(SESSION_SECONDS)

# ================= 6. Dừng ghi =================
eyetracker.unsubscribe_from(tr.EYETRACKER_GAZE_DATA, gaze_data_callback)
stop_flag.set()
scroll_thread.join()

print(f"Đã ghi được {len(gaze_log)} mẫu gaze trong {gaze_log[-1][0]:.2f}s (đồng hồ Python)")

with open(os.path.join(output_dir, "gaze_data.csv"), "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["elapsed_sec", "left_x", "left_y", "left_valid", "right_x", "right_y", "right_valid"])
    writer.writerows(gaze_log)

# ================= 7. Đo DPR / sticky header =================
dpr = driver.execute_script("return window.devicePixelRatio;")
print(f"Device Pixel Ratio: {dpr}")

header_height_css = driver.execute_script("""
    const all = document.querySelectorAll('*');
    let maxBottom = 0;
    for (const el of all) {
        const style = window.getComputedStyle(el);
        if (style.position === 'fixed' || style.position === 'sticky') {
            const rect = el.getBoundingClientRect();
            if (rect.top <= 5 && rect.height > 0 && rect.height < 300) {
                maxBottom = Math.max(maxBottom, rect.bottom);
            }
        }
    }
    return maxBottom;
""")
print(f"Vùng sticky/fixed (CSS px): {header_height_css}px")

with open(os.path.join(output_dir, "screen_info.json"), "w") as f:
    json.dump({
        "SCREEN_W": SCREEN_W, "SCREEN_H": SCREEN_H,
        "DPR": dpr, "HEADER_HEIGHT_CSS": header_height_css,
    }, f)

# ================= 8. Chụp ảnh toàn trang bằng CDP =================
driver.execute_script("window.scrollTo(0, document.body.scrollHeight); window.dispatchEvent(new Event('scroll'));")
time.sleep(2)
driver.execute_script("window.scrollTo(0, 0); window.dispatchEvent(new Event('scroll'));")
time.sleep(2)

page_width = driver.execute_script("return document.documentElement.scrollWidth")
page_height = driver.execute_script("return document.documentElement.scrollHeight")
print(f"Kích thước trang (CSS px): {page_width}x{page_height}")

driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
    "width": page_width, "height": page_height,
    "deviceScaleFactor": dpr, "mobile": False,
})
result = driver.execute_cdp_cmd("Page.captureScreenshot", {
    "captureBeyondViewport": True, "format": "png",
})
with open(os.path.join(output_dir, "fullpage_screenshot.png"), "wb") as f:
    f.write(base64.b64decode(result["data"]))

driver.quit()

with open(os.path.join(output_dir, "scroll_log.csv"), "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["elapsed_sec", "scrollY"])
    writer.writerows(scroll_log)

print(f"Xong. Dữ liệu lưu trong folder: {output_dir}/")
print(f"  - gaze_data.csv ({len(gaze_log)} mẫu)")
print(f"  - fullpage_screenshot.png ({page_width}x{page_height} CSS px, DPR {dpr})")