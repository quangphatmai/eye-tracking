from titta import Titta
from psychopy import visual
import time
import os
import sys

PARTICIPANT_ID = sys.argv[1] if len(sys.argv) > 1 else "participant_01"
CALIBRATION_DIR = os.path.join(PARTICIPANT_ID, f"{PARTICIPANT_ID}_calibration")
os.makedirs(CALIBRATION_DIR, exist_ok=True)

settings = Titta.get_defaults("Tobii Pro Nano")
settings.N_CAL_TARGETS = 5
settings.FILENAME = PARTICIPANT_ID
settings.DATA_STORAGE_PATH = CALIBRATION_DIR

tracker = Titta.Connect(settings)
tracker.init()

win = visual.Window(monitor="testMonitor", fullscr=True, units="pix")
tracker.calibrate(win)

recording_text = visual.TextStim(
    win,
    text="Recording in progress...\nPlease look around the screen.",
    color="white",
    height=40
)
recording_text.draw()
win.flip()

tracker.start_recording(gaze=True)
print("Recording... Look around for few seconds.")
time.sleep(15)
tracker.stop_recording(gaze=True)

settings.DATA_STORAGE_PATH = PARTICIPANT_ID
tracker.save_data(PARTICIPANT_ID)
win.close()
print(f"Đã lưu gaze data trong '{PARTICIPANT_ID}/', calibration images trong '{CALIBRATION_DIR}/'.")