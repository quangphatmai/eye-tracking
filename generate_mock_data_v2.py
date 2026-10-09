import numpy as np

np.random.seed(42)
SAMPLE_RATE_HZ = 120
FIXATION_POINTS = [
    (0.3, 0.3), (0.7, 0.3), (0.7, 0.7), (0.3, 0.7),
    (0.5, 0.5), (0.2, 0.6), (0.8, 0.4),
]
FIXATION_DURATION_MS = 400
SACCADE_DURATION_MS = 40

samples = []
t = 0.0
for i, (fx, fy) in enumerate(FIXATION_POINTS):
    n_fix = int(FIXATION_DURATION_MS / 1000 * SAMPLE_RATE_HZ)
    for _ in range(n_fix):
        samples.append((t, fx + np.random.normal(0, 0.003), fy + np.random.normal(0, 0.003)))
        t += 1000 / SAMPLE_RATE_HZ
    if i < len(FIXATION_POINTS) - 1:
        nx, ny = FIXATION_POINTS[i + 1]
        n_sac = max(2, int(SACCADE_DURATION_MS / 1000 * SAMPLE_RATE_HZ))
        for s in range(n_sac):
            frac = (s + 1) / n_sac
            samples.append((t, fx + (nx - fx) * frac, fy + (ny - fy) * frac))
            t += 1000 / SAMPLE_RATE_HZ

samples = np.array(samples)
header = "system_time_stamp\tleft_gaze_point_on_display_area_x\tleft_gaze_point_on_display_area_y\tright_gaze_point_on_display_area_x\tright_gaze_point_on_display_area_y"
rows = np.column_stack([samples[:,0]*1000, samples[:,1], samples[:,2], samples[:,1], samples[:,2]])
np.savetxt("participant_mock2.tsv", rows, delimiter="\t", header=header, comments="")
print("Đã tạo participant_mock2.tsv — có cả fixation lẫn saccade rõ ràng.")