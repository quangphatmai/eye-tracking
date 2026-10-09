path = "PyGazeAnalyser/pygazeanalyser/gazeplotter.py"

with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Dòng 235 (index 234): imshow trong draw_heatmap -> thêm interpolation mượt hơn
old_imshow = "ax.imshow(heatmap, cmap='jet', alpha=alpha)"
new_imshow = "ax.imshow(heatmap, cmap='jet', alpha=alpha, interpolation='bicubic')"
if old_imshow in lines[234]:
    lines[234] = lines[234].replace(old_imshow, new_imshow)
    print("Đã sửa dòng 235 (imshow).")
else:
    print(f"CẢNH BÁO: dòng 235 không khớp, nội dung thật: {lines[234]!r}")

# Dòng 242 (index 241): savefig trong draw_heatmap -> thêm dpi cao hơn
old_savefig = "fig.savefig(savefilename)"
new_savefig = "fig.savefig(savefilename, dpi=150)"
if old_savefig in lines[241]:
    lines[241] = lines[241].replace(old_savefig, new_savefig)
    print("Đã sửa dòng 242 (savefig trong draw_heatmap).")
else:
    print(f"CẢNH BÁO: dòng 242 không khớp, nội dung thật: {lines[241]!r}")

# Dòng 350 (index 349): savefig trong draw_scanpath -> thêm dpi cao hơn luôn, cho nét
if old_savefig in lines[349]:
    lines[349] = lines[349].replace(old_savefig, new_savefig)
    print("Đã sửa dòng 350 (savefig trong draw_scanpath).")
else:
    print(f"CẢNH BÁO: dòng 350 không khớp, nội dung thật: {lines[349]!r}")

with open(path, "w", encoding="utf-8") as f:
    f.writelines(lines)

print("Hoàn tất.")