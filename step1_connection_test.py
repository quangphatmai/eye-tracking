import tobii_research as tr
import time

found_eyetrackers = tr.find_all_eyetrackers()
if not found_eyetrackers:
    raise RuntimeError("Không tìm thấy Tobii eye tracker — kiểm tra lại cáp USB và license.")

eyetracker = found_eyetrackers[0]
print(f"Đã kết nối: {eyetracker.model} ({eyetracker.serial_number})")

def gaze_data_callback(gaze_data):
    left = gaze_data["left_gaze_point_on_display_area"]
    right = gaze_data["right_gaze_point_on_display_area"]
    ts = gaze_data["system_time_stamp"]
    print(f"t={ts}  L={left}  R={right}")

eyetracker.subscribe_to(tr.EYETRACKER_GAZE_DATA, gaze_data_callback, as_dictionary=True)
time.sleep(5)
eyetracker.unsubscribe_from(tr.EYETRACKER_GAZE_DATA, gaze_data_callback)
print("Xong.")