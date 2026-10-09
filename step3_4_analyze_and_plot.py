"""Step 3-4: detect fixations in a Tobii-style gaze TSV and plot a scanpath + heatmap.

Usage:
    python step3_4_analyze_and_plot.py [input.tsv] [participant_id]
    e.g. python step3_4_analyze_and_plot.py participant_custom.tsv p01

The same script works for mock data and for real Tobii exports (same column names,
system_time_stamp in microseconds, gaze normalised to 0-1, invalid samples as NaN).

Outputs (in the current folder):
    scanpath_<participant_id>.png  - order and direction of eye movements, with a fixation table
    heatmap_<participant_id>.png   - where gaze time was concentrated on the screen
"""
import sys
sys.path.append("PyGazeAnalyser")

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from matplotlib.colors import LinearSegmentedColormap
from pygazeanalyser.detectors import fixation_detection

# Import of the library resets the font to "Ubuntu"; go back to a font that exists.
matplotlib.rcParams["font.family"] = "DejaVu Sans"

# ------------------------------------------------------------------ settings
import os
TSV_FILE = sys.argv[1] if len(sys.argv) > 1 else "participant_custom.tsv"
PARTICIPANT = sys.argv[2] if len(sys.argv) > 2 else "p01"
OUT_DIR = os.path.dirname(TSV_FILE) or "."   # lưu ảnh cùng folder với file tsv input
SCANPATH_PNG = os.path.join(OUT_DIR, f"scanpath_{PARTICIPANT}.png")
HEATMAP_PNG = os.path.join(OUT_DIR, f"heatmap_{PARTICIPANT}.png")
SCREEN_W, SCREEN_H = 1920, 1080   # pixels; gaze columns are normalised 0-1
MAX_DIST_PX = 25                  # samples within this distance belong to one fixation
MIN_DUR_MS = 50                   # shorter fixations are discarded
MERGE_DIST_PX = 60                # consecutive fixations closer than this count as one
HEATMAP_SIGMA_PX = 70             # blur radius of the heatmap

# ------------------------------------------------------------------ load data
data = np.genfromtxt(TSV_FILE, delimiter="\t", names=True)
print("Columns in file:", data.dtype.names)

lx = data["left_gaze_point_on_display_area_x"]
ly = data["left_gaze_point_on_display_area_y"]
rx = data["right_gaze_point_on_display_area_x"]
ry = data["right_gaze_point_on_display_area_y"]


def valid(a, b):
    """A sample is valid when the value is finite and on the display (0-1)."""
    return np.isfinite(a) & np.isfinite(b) & (a >= 0) & (a <= 1) & (b >= 0) & (b <= 1)


lok, rok = valid(lx, ly), valid(rx, ry)
# Real Tobii exports also carry per-eye validity flags (1 = valid); use them when present.
for col in ("left_gaze_point_validity", "right_gaze_point_validity"):
    if col in data.dtype.names:
        flag = data[col] == 1
        if col.startswith("left"):
            lok &= flag
        else:
            rok &= flag
# Average both eyes; if only one eye is valid use that eye; otherwise mark missing.
gx = np.where(lok & rok, (lx + rx) / 2, np.where(lok, lx, np.where(rok, rx, np.nan))) * SCREEN_W
gy = np.where(lok & rok, (ly + ry) / 2, np.where(lok, ly, np.where(rok, ry, np.nan))) * SCREEN_H
t_ms = data["system_time_stamp"] / 1000.0   # microseconds -> milliseconds
t_ms = t_ms - t_ms[0]
present = np.isfinite(gx) & np.isfinite(gy)

duration_s = t_ms[-1] / 1000.0
rate_hz = 1000.0 / np.median(np.diff(t_ms))
print(f"{len(t_ms)} samples, {duration_s:.2f} s, ~{rate_hz:.0f} Hz, "
      f"{(~present).sum()} samples without valid gaze")

# ------------------------------------------------------------------ fixations
# Missing samples are set to 0 because the detector treats 0.0 as "missing".
_, raw_fix = fixation_detection(np.where(present, gx, 0.0), np.where(present, gy, 0.0),
                                t_ms, missing=0.0, maxdist=MAX_DIST_PX, mindur=MIN_DUR_MS)


def fixation_from_window(start, end):
    """Fixation centre = mean of the real gaze samples inside its time window."""
    m = present & (t_ms >= start) & (t_ms <= end)
    return [start, end, end - start, gx[m].mean(), gy[m].mean(), m.sum()]


fixations = [fixation_from_window(f[0], f[1]) for f in raw_fix]

# Merge consecutive fixations that are practically at the same spot.
merged = []
for f in fixations:
    if merged and np.hypot(f[3] - merged[-1][3], f[4] - merged[-1][4]) <= MERGE_DIST_PX:
        merged[-1] = fixation_from_window(merged[-1][0], f[1])
    else:
        merged.append(f)
fixations = merged
print(f"{len(raw_fix)} raw fixations -> {len(fixations)} after merging (<= {MERGE_DIST_PX}px apart)")
if not fixations:
    sys.exit("No fixations found - check the thresholds or the input file.")

fx = np.array([f[3] for f in fixations])
fy = np.array([f[4] for f in fixations])
f_start = np.array([f[0] for f in fixations]) / 1000.0     # s
f_dur = np.array([f[2] for f in fixations])                # ms
n = len(fixations)

# Saccades = jumps between consecutive fixations.
sac_len = np.hypot(np.diff(fx) / SCREEN_W, np.diff(fy) / SCREEN_H)   # normalised units
sac_dur = (np.array([f[0] for f in fixations[1:]]) - np.array([f[1] for f in fixations[:-1]]))

# ------------------------------------------------------------------ style
BG, PANEL, GRID, TXT, MUTED, EDGE = "#0f1117", "#151923", "#262c3a", "#eef0f5", "#98a2b8", "#3a4256"
# Plain "plasma" starts almost black at t=0, so the earliest saccades/fixations
# were rendering nearly invisible on this near-black background. Trim the
# colormap so even the very first arrow uses a bright, legible colour, while
# still running the same gradient up to yellow at the end of the recording.
_plasma = plt.get_cmap("plasma")
cmap = LinearSegmentedColormap.from_list(
    "plasma_bright", _plasma(np.linspace(0.25, 1.0, 256))
)
norm = plt.Normalize(0, duration_s)


def style_screen(ax, title):
    """Draw the screen rectangle, grid and axes shared by both figures."""
    ax.set_facecolor(PANEL)
    ax.add_patch(Rectangle((0, 0), SCREEN_W, SCREEN_H, fill=False, ec=EDGE, lw=1.5, zorder=2))
    ticks = np.linspace(0, 1, 11)            # axes show normalised gaze coordinates 0-1
    for t in ticks:
        ax.axvline(t * SCREEN_W, color=GRID, lw=0.6, zorder=0)
        ax.axhline(t * SCREEN_H, color=GRID, lw=0.6, zorder=0)
    ax.set_xticks(ticks * SCREEN_W)
    ax.set_xticklabels([f"{t:.1f}" for t in ticks])
    ax.set_yticks(ticks * SCREEN_H)
    ax.set_yticklabels([f"{t:.1f}" for t in ticks])
    ax.set_xlim(-30, SCREEN_W + 30)
    ax.set_ylim(SCREEN_H + 30, -30)          # (0,0) is the top-left of the screen
    ax.set_aspect("equal")
    ax.set_xlabel("Gaze x  (0 = left edge, 1 = right edge)", color=TXT)
    ax.set_ylabel("Gaze y  (0 = top edge, 1 = bottom edge)", color=TXT)
    ax.tick_params(colors=MUTED)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, color=TXT, fontsize=13, loc="left", pad=10)


# ------------------------------------------------------------------ scanpath
fig = plt.figure(figsize=(17, 8.6), dpi=120, facecolor=BG)
ax = fig.add_axes([0.05, 0.16, 0.62, 0.66])
cax = fig.add_axes([0.05, 0.075, 0.62, 0.017])
side = fig.add_axes([0.70, 0.16, 0.28, 0.66])
side.axis("off")

style_screen(ax, "Where the eyes went (numbers = order of fixations)")

# faint raw gaze samples as context
ax.plot(gx[present], gy[present], color=MUTED, lw=0.7, alpha=0.30, zorder=1)
ax.scatter(gx[present], gy[present], s=4, color=MUTED, alpha=0.30, linewidths=0, zorder=1)

# saccade arrows coloured by time, slightly curved so return trips stay visible
for i in range(n - 1):
    ax.annotate("", xy=(fx[i + 1], fy[i + 1]), xytext=(fx[i], fy[i]), zorder=3,
                arrowprops=dict(arrowstyle="-|>,head_length=0.7,head_width=0.35",
                                color=cmap(norm((f_start[i] + f_start[i + 1]) / 2)),
                                lw=2.2, alpha=0.95, connectionstyle="arc3,rad=0.12",
                                shrinkA=14, shrinkB=14))

# fixation circles: area ~ duration, colour ~ time since recording start.
# Very brief fixations (just passing through) get a small plain dot instead of
# a numbered circle, so the numbers don't pile up on top of each other.
MIN_LABEL_DUR_MS = 200
labelled = f_dur >= MIN_LABEL_DUR_MS
sizes = 120 + f_dur * 1.1

ax.scatter(fx[labelled], fy[labelled], s=sizes[labelled] * 1.9,
           color=cmap(norm(f_start[labelled])), alpha=0.18, linewidths=0, zorder=4)
ax.scatter(fx[labelled], fy[labelled], s=sizes[labelled],
           color=cmap(norm(f_start[labelled])), ec="white", lw=1.4, alpha=0.95, zorder=5)
for i in np.where(labelled)[0]:
    ax.text(fx[i], fy[i], str(i + 1), color="white", fontsize=11, fontweight="bold",
            ha="center", va="center", zorder=6)

ax.scatter(fx[~labelled], fy[~labelled], s=45, color=cmap(norm(f_start[~labelled])),
           ec="white", lw=0.6, alpha=0.55, zorder=5)

label_box = dict(boxstyle="round,pad=0.3", fc=BG, ec=EDGE, alpha=0.95)
ax.annotate("START", (fx[0], fy[0]), xytext=(0, -30), textcoords="offset points",
            color=TXT, fontsize=9, ha="center", bbox=label_box, zorder=7)
ax.annotate("END", (fx[-1], fy[-1]), xytext=(34, 0), textcoords="offset points",
            color=TXT, fontsize=9, ha="left", va="center", bbox=label_box, zorder=7)

ax.legend(handles=[
    Line2D([], [], marker="o", ls="", mfc="#c8cdd8", mec="white", ms=13, label="Fixation (eyes rest here)"),
    Line2D([], [], marker="o", ls="", mfc="#c8cdd8", mec="white", ms=7, label="Bigger circle = longer look"),
    Line2D([], [], marker="o", ls="", mfc="#c8cdd8", mec="#c8cdd8", ms=5, alpha=0.55,
           label=f"Small dot = brief fixation (<{MIN_LABEL_DUR_MS}ms), no number"),
    Line2D([], [], color="#c8cdd8", lw=2, label="Saccade (quick jump to next point)"),
    Line2D([], [], color=MUTED, lw=1, alpha=0.6, label="Raw gaze samples"),
], loc="lower right", facecolor=BG, edgecolor=EDGE, labelcolor=TXT, fontsize=9, framealpha=0.95)

cb = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=cmap), cax=cax, orientation="horizontal")
cb.set_label("Time since recording start (seconds): dark = early, bright = late", color=TXT)
cb.ax.tick_params(colors=MUTED)
cb.outline.set_visible(False)

# fixation table
side.set_xlim(0, 1)
side.set_ylim(0, 1)
side.text(0, 1.0, "Fixation sequence", color=TXT, fontsize=13, va="top")

HEADER_Y = 0.955
FIRST_ROW_Y = 0.915
LAST_USABLE_Y = 0.02
AVAILABLE_H = FIRST_ROW_Y - LAST_USABLE_Y
MIN_ROW_STEP = 0.016
MIN_FONT = 6.5
MAX_FONT = 9.5

rows_per_col_1col = int(AVAILABLE_H / MIN_ROW_STEP) + 1
if n <= rows_per_col_1col:
    n_cols = 1
    rows_per_col = n
    row_step = min(AVAILABLE_H / max(n - 1, 1), 0.032) if n > 1 else 0.032
    font_size = MAX_FONT if n <= 20 else max(MIN_FONT, MAX_FONT * 20 / n)
else:
    n_cols = 2
    rows_per_col = -(-n // n_cols)
    row_step = AVAILABLE_H / max(rows_per_col - 1, 1)
    font_size = MIN_FONT

col_gap = 0.56 if n_cols > 1 else 0.0
header = f"{'#':>2}  {'Start':>7}  {'Duration':>9}  {'Jump to next':>13}"
for c in range(n_cols):
    side.text(c * col_gap, HEADER_Y, header, color=MUTED, fontsize=font_size,
              va="top", family="monospace")

for i in range(n):
    jump = f"{sac_len[i]:.2f}" if i < n - 1 else "-"
    col, row = divmod(i, rows_per_col)
    side.text(col * col_gap, FIRST_ROW_Y - row * row_step,
              f"{i + 1:>2}  {f_start[i]:>6.2f}s  {f_dur[i]:>7.0f}ms  {jump:>13}",
              color=TXT, fontsize=font_size, va="top", family="monospace")

fig.text(0.05, 0.935, "Scanpath: eye movement over the screen", color=TXT, fontsize=21, fontweight="bold")
fig.text(0.05, 0.895,
         f"{n} fixations  |  {duration_s:.1f} s recorded at ~{rate_hz:.0f} Hz  |  "
         f"mean fixation {f_dur.mean():.0f} ms"
         + (f"  |  mean jump {sac_len.mean():.2f} (fraction of screen)" if n > 1 else ""),
         color=MUTED, fontsize=11)
fig.savefig(SCANPATH_PNG, facecolor=BG)
plt.close(fig)

# ------------------------------------------------------------------ heatmap
STEP = 4
xs = np.arange(0, SCREEN_W, STEP) + STEP / 2
ys = np.arange(0, SCREEN_H, STEP) + STEP / 2
XX, YY = np.meshgrid(xs, ys)
density = np.zeros_like(XX, dtype=float)
for x0, y0, d in zip(fx, fy, f_dur):     # each fixation adds a Gaussian weighted by its duration
    density += d * np.exp(-((XX - x0) ** 2 + (YY - y0) ** 2) / (2 * HEATMAP_SIGMA_PX ** 2))
density /= density.max()
# Gamma-lift the intensity: with a plain linear scale, any fixation much
# shorter than the longest one collapsed to near-black on "inferno" and
# all but disappeared. Compressing with **0.55 keeps the same ranking
# (longer look = hotter) but makes shorter fixations clearly visible too.
density = np.power(density, 0.55)
density = np.ma.masked_less(density, 0.15)   # hide near-zero areas so the screen stays visible

fig = plt.figure(figsize=(16, 8.6), dpi=120, facecolor=BG)
ax = fig.add_axes([0.05, 0.10, 0.80, 0.72])
cax = fig.add_axes([0.88, 0.10, 0.015, 0.72])
style_screen(ax, "Longer looks are hotter")
im = ax.imshow(density, extent=[0, SCREEN_W, SCREEN_H, 0], cmap="inferno", vmin=0, vmax=1,
               alpha=0.95, zorder=1, interpolation="bilinear")
ax.scatter(fx, fy, s=28, color="white", ec=BG, lw=0.8, zorder=3)
for i in range(n):
    ax.text(fx[i], fy[i], f" {i + 1}", color="white", fontsize=9, va="bottom", zorder=4)
ax.set_xlim(-30, SCREEN_W + 30)
ax.set_ylim(SCREEN_H + 30, -30)
cb = fig.colorbar(im, cax=cax)
cb.set_label("Relative attention (time spent looking)", color=TXT)
cb.set_ticks([0.05, 0.5, 1.0])
cb.set_ticklabels(["low", "medium", "high"])
cb.ax.tick_params(colors=MUTED)
cb.outline.set_visible(False)
fig.text(0.05, 0.935, "Heatmap: where attention was concentrated", color=TXT, fontsize=21, fontweight="bold")
fig.text(0.05, 0.895, f"Fixations weighted by duration  |  {n} fixations  |  "
         f"white dots = fixation centres (numbered in order)", color=MUTED, fontsize=11)
fig.savefig(HEATMAP_PNG, facecolor=BG)
plt.close(fig)

print(f"Saved {SCANPATH_PNG} and {HEATMAP_PNG}")
