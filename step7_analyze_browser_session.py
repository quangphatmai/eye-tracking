import csv
import json
import numpy as np
import cv2
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import sys

sys.path.append("PyGazeAnalyser")
from pygazeanalyser.detectors import fixation_detection, saccade_detection

# ================= CẤU HÌNH =================
PARTICIPANT_DIR = "participant_01"   # đổi đúng folder participant mới nhất

GAZE_CSV = f"{PARTICIPANT_DIR}/gaze_data.csv"
SCROLL_CSV = f"{PARTICIPANT_DIR}/scroll_log.csv"
SCREENSHOT = f"{PARTICIPANT_DIR}/fullpage_screenshot.png"
SCREEN_INFO = f"{PARTICIPANT_DIR}/screen_info.json"

with open(SCREEN_INFO) as f:
    info = json.load(f)
SCREEN_W, SCREEN_H = info["SCREEN_W"], info["SCREEN_H"]
DPR = info["DPR"]
HEADER_HEIGHT_CSS = info["HEADER_HEIGHT_CSS"]
HEADER_HEIGHT_PHYS = HEADER_HEIGHT_CSS * DPR
print(f"DPR={DPR}, header={HEADER_HEIGHT_CSS}css px = {HEADER_HEIGHT_PHYS:.0f}px vật lý")

# ---- Đọc gaze_data.csv (đã ghi sẵn bằng đồng hồ Python, cùng hệ với scroll_log) ----
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

# ---- Đọc scroll log (cùng đồng hồ Python với gaze, không cần quy đổi gì nữa) ----
scroll_times, scroll_offsets = [], []
with open(SCROLL_CSV) as f:
    for row in csv.DictReader(f):
        scroll_times.append(float(row["elapsed_sec"]))
        scroll_offsets.append(float(row["scrollY"]))
scroll_times = np.array(scroll_times)
scroll_offsets_css = np.array(scroll_offsets)
print(f"Đọc được {len(scroll_times)} mốc scroll.")

matched_scroll_css = np.interp(t, scroll_times, scroll_offsets_css)
matched_scroll_phys = matched_scroll_css * DPR

y_page = np.where(y <= HEADER_HEIGHT_PHYS, y, y + matched_scroll_phys)
x_page = x

valid = ~(np.isnan(x_page) | np.isnan(y_page))
x_page, y_page = x_page[valid], y_page[valid]
time_valid_ms = t[valid] * 1000.0   # fixation_detection cần đơn vị ms
print(f"Còn lại {len(x_page)} điểm gaze hợp lệ.")

Sfix, Efix = fixation_detection(x_page, y_page, time_valid_ms, maxdist=25, mindur=50)
Ssac, Esac = saccade_detection(x_page, y_page, time_valid_ms, maxvel=40, maxacc=340)
print(f"Phát hiện {len(Efix)} fixations, {len(Esac)} saccades.")

bg = cv2.imread(SCREENSHOT)
page_h, page_w = bg.shape[:2]
print(f"Kích thước ảnh toàn trang: {page_w}x{page_h}")

# ================= HEATMAP =================
RADIUS = 110
BLUR_SIGMA = 55
MAX_ALPHA = 0.65

canvas = np.zeros((page_h, page_w), dtype=np.float32)
for e in Efix:
    _, _, dur, fx_, fy_ = e
    if 0 <= int(fx_) < page_w and 0 <= int(fy_) < page_h:
        temp = np.zeros_like(canvas)
        cv2.circle(temp, (int(fx_), int(fy_)), RADIUS, 1.0, -1)
        canvas += temp * (dur / 150.0)

canvas = cv2.GaussianBlur(canvas, (0, 0), BLUR_SIGMA)
nonzero = canvas[canvas > 0]
clip_val = np.percentile(nonzero, 97) if nonzero.size else 1.0
canvas_clipped = np.clip(canvas, 0, clip_val)
norm = canvas_clipped / clip_val if clip_val > 0 else canvas_clipped
norm_boosted = np.power(norm, 0.5)

heat_u8 = np.clip(norm_boosted * 255, 0, 255).astype(np.uint8)
heat_color = cv2.applyColorMap(heat_u8, cv2.COLORMAP_JET)
alpha = np.clip(norm_boosted, 0, 1)[..., None] * MAX_ALPHA
blended = (bg.astype(np.float32) * (1 - alpha) + heat_color.astype(np.float32) * alpha).astype(np.uint8)
cv2.imwrite(f"{PARTICIPANT_DIR}/heatmap_fullpage.png", blended)

# ================= SCANPATH =================
fx = np.array([e[3] for e in Efix])
fy = np.array([e[4] for e in Efix])
fdur = np.array([e[2] for e in Efix])
fstart_sec = (np.array([e[0] for e in Efix]) - time_valid_ms[0]) / 1000.0
n = len(fx)

BG, GRID, TEXT, MUTED = "#0b0f19", "#242c3d", "#c7ccd9", "#7c8699"
plt.rcParams.update({"figure.facecolor": BG, "savefig.facecolor": BG, "text.color": TEXT, "font.family": "DejaVu Sans"})

fig_w = 16
fig_h = fig_w * (page_h / page_w) + 2
fig = plt.figure(figsize=(fig_w, fig_h), dpi=150)
gs = fig.add_gridspec(3, 1, height_ratios=[0.35, page_h / page_w * fig_w, 0.25], hspace=0.04)

ax_head = fig.add_subplot(gs[0]); ax_head.axis("off")
ax_head.text(0, 0.7, "Scanpath: eye movement over the page", fontsize=20, fontweight="bold", color="white", transform=ax_head.transAxes)
total_sec = (time_valid_ms[-1] - time_valid_ms[0]) / 1000.0 if len(time_valid_ms) else 0
mean_jump = np.mean(np.hypot(np.diff(fx), np.diff(fy))) if n > 1 else 0
ax_head.text(0, 0.1, f"{n} fixations  |  {total_sec:.1f}s recorded  |  mean fixation {np.mean(fdur):.0f}ms  |  mean jump {mean_jump:.0f}px",
             fontsize=12, color=MUTED, transform=ax_head.transAxes)

ax = fig.add_subplot(gs[1])
img_rgb = cv2.cvtColor(bg, cv2.COLOR_BGR2RGB)
ax.imshow(img_rgb, extent=[0, page_w, page_h, 0])

cmap = plt.get_cmap("plasma")
norm_t = plt.Normalize(vmin=0, vmax=max(total_sec, 0.001))

for i in range(n - 1):
    color = cmap(norm_t((fstart_sec[i] + fstart_sec[i + 1]) / 2))
    ax.annotate("", xy=(fx[i + 1], fy[i + 1]), xytext=(fx[i], fy[i]),
                arrowprops=dict(arrowstyle="-|>", color=color, lw=2.2, alpha=0.9, shrinkA=14, shrinkB=14))

sizes = np.clip(fdur / 1.5, 300, 4000)
ax.scatter(fx, fy, s=sizes, c=fstart_sec, cmap="plasma", norm=norm_t, edgecolors="white", linewidths=1.6, zorder=5)
for i in range(n):
    ax.annotate(str(i + 1), (fx[i], fy[i]), color="white", fontsize=12, fontweight="bold", ha="center", va="center", zorder=6)

if n > 0:
    ax.annotate("START", (fx[0], fy[0]), xytext=(fx[0], fy[0] - 40), color=TEXT, fontsize=11, ha="center",
                bbox=dict(boxstyle="round,pad=0.3", fc=BG, ec=GRID))
    ax.annotate("END", (fx[-1], fy[-1]), xytext=(fx[-1] + 40, fy[-1]), color=TEXT, fontsize=11, ha="left",
                bbox=dict(boxstyle="round,pad=0.3", fc=BG, ec=GRID))

ax.set_xlim(0, page_w); ax.set_ylim(page_h, 0); ax.axis("off")
ax.legend(handles=[
    mpatches.Patch(color="none", label="\u25CF  Fixation (eyes rest here)"),
    mpatches.Patch(color="none", label="\u25CF  Bigger circle = longer look"),
    mpatches.Patch(color="none", label="\u2192  Saccade (quick jump)"),
], loc="upper right", frameon=True, facecolor=BG, edgecolor=GRID, fontsize=10, labelcolor=TEXT)

ax_cbar = fig.add_subplot(gs[2])
cb = plt.colorbar(plt.cm.ScalarMappable(norm=norm_t, cmap=cmap), cax=ax_cbar, orientation="horizontal")
cb.set_label("Time since recording start (seconds): dark = early, bright = late", color=MUTED, fontsize=11)
cb.outline.set_edgecolor(GRID)

fig.savefig(f"{PARTICIPANT_DIR}/scanpath_fullpage.png", dpi=150, bbox_inches="tight")
plt.close(fig)

print(f"\nXong. Đã lưu trong {PARTICIPANT_DIR}/: heatmap_fullpage.png, scanpath_fullpage.png")