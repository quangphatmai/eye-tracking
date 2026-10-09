import numpy as np

SAMPLE_RATE_HZ = 120

# Mỗi điểm: (x, y) theo tỉ lệ 0..1 của màn hình (0,0 = góc trên trái, 1,1 = góc dưới phải)
# và thời gian "nhìn" tại điểm đó, đơn vị mili-giây.
# Tự sửa danh sách này để đổi hình dạng heat map theo ý muốn.
FIXATION_POINTS = [
    ((0.1, 0.9), 100),
    ((0.2, 0.8), 200),
    ((0.3, 0.7), 300),
    ((0.4, 0.6), 400),   
    ((0.5, 0.5), 500),   
    ((0.6, 0.4), 600),
    ((0.7, 0.3), 700),
    ((0.8, 0.2), 800),
]

samples = []
t = 0.0
for (fx, fy), dur_ms in FIXATION_POINTS:
    n = int(dur_ms / 1000 * SAMPLE_RATE_HZ)
    for _ in range(n):
        samples.append((t, fx + np.random.normal(0, 0.004), fy + np.random.normal(0, 0.004)))
        t += 1000 / SAMPLE_RATE_HZ

samples = np.array(samples)
header = "system_time_stamp\tleft_gaze_point_on_display_area_x\tleft_gaze_point_on_display_area_y\tright_gaze_point_on_display_area_x\tright_gaze_point_on_display_area_y"
rows = np.column_stack([samples[:,0]*1000, samples[:,1], samples[:,2], samples[:,1], samples[:,2]])
np.savetxt("participant_custom.tsv", rows, delimiter="\t", header=header, comments="")
print("Đã tạo participant_custom.tsv")