path = "PyGazeAnalyser/pygazeanalyser/gazeplotter.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

original = content
content = content.replace(
    "lowbound = numpy.percentile(heatmap[heatmap>0], 3)",
    "lowbound = numpy.percentile(heatmap[heatmap>0], 1)"
)

if content == original:
    print("KHÔNG có gì thay đổi — có thể ngưỡng hiện tại không phải percentile 3, kiểm tra lại bên dưới.")
else:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("Đã patch xong: ngưỡng cắt heatmap hạ xuống percentile 1%.")