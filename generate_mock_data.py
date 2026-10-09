import numpy as np

N_SAMPLES = 2000
SAMPLE_RATE_HZ = 120  # Tobii Pro Nano ghi ở tần số này

t = np.arange(N_SAMPLES) / SAMPLE_RATE_HZ * 1000  # mốc thời gian, đơn vị ms
system_time_stamp = t * 1000  # giả lập cột gốc của Tobii là microseconds

# Giả lập mắt di chuyển theo hình sin qua lại màn hình (0..1, normalized)
x_norm = 0.5 + 0.35 * np.sin(t / 900)
y_norm = 0.5 + 0.25 * np.sin(t / 1400 + 1)

header = "system_time_stamp\tleft_gaze_point_on_display_area_x\tleft_gaze_point_on_display_area_y\tright_gaze_point_on_display_area_x\tright_gaze_point_on_display_area_y"
rows = np.column_stack([system_time_stamp, x_norm, y_norm, x_norm, y_norm])
np.savetxt("participant_mock.tsv", rows, delimiter="\t", header=header, comments="")
print("Đã tạo participant_mock.tsv — dùng file này thay cho file thật ở Phần 8.")