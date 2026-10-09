path = "PyGazeAnalyser/pygazeanalyser/gazeplotter.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

original = content
content = content.replace(
    "gwh = 80",
    "gwh = 150"
)

if content == original:
    print("KHÔNG có gì thay đổi.")
else:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("Đã patch xong: gwh=150 — vùng sáng rộng và mượt hơn.")