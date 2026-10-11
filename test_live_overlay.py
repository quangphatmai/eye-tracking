import asyncio
import json
import math
import threading
import queue
import tkinter as tk
import websockets
import mss
from PIL import Image, ImageTk
from screeninfo import get_monitors

WS_URL = "ws://localhost:8765"
SOURCE_W, SOURCE_H = 1920, 1080  # phải khớp đúng SCREEN_W/H trong step5_websocket_server.py

gaze_queue = queue.Queue()


async def listen():
    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                print("Đã kết nối tới", WS_URL)
                async for message in ws:
                    data = json.loads(message)
                    gaze_queue.put((data["x"], data["y"]))
        except Exception as e:
            print("Mất kết nối, thử lại sau 2 giây:", e)
            await asyncio.sleep(2)


def start_websocket_thread():
    threading.Thread(target=lambda: asyncio.run(listen()), daemon=True).start()


def get_monitor_geometries():
    monitors = get_monitors()
    if len(monitors) < 2:
        print("Chỉ phát hiện 1 màn hình.")
        return None, None
    primary = next((m for m in monitors if m.is_primary), monitors[0])
    secondary = next((m for m in monitors if not m.is_primary), monitors[1])
    print(f"Primary (participant): {primary}")
    print(f"Secondary (observer): {secondary}")
    return primary, secondary


class LiveOverlay:
    def __init__(self, root, primary, secondary):
        self.root = root
        self.primary = primary
        self.secondary = secondary

        # Bước 1: bỏ fullscreen/decoration trước, đặt đúng vị trí trên monitor phụ
        self.root.overrideredirect(True)
        self.root.geometry(f"{secondary.width}x{secondary.height}+{secondary.x}+{secondary.y}")
        self.root.update_idletasks()  # ép Windows xử lý xong việc "di chuyển" trước khi làm gì tiếp

        self.canvas = tk.Canvas(root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.root.bind("<Escape>", lambda e: root.destroy())

        self.sct = mss.mss()
        self.capture_region = {
            "left": primary.x,
            "top": primary.y,
            "width": primary.width,
            "height": primary.height,
        }

        self.image_item = self.canvas.create_image(0, 0, anchor="nw")
        self.photo = None
        self.dot = None

        self.update_mirror()
        self.poll_gaze()

    def update_mirror(self):
        frame = self.sct.grab(self.capture_region)
        pil_img = Image.frombytes("RGB", frame.size, frame.bgra, "raw", "BGRX")
        pil_img = pil_img.resize((self.secondary.width, self.secondary.height), Image.NEAREST)
        self.photo = ImageTk.PhotoImage(pil_img)
        self.canvas.itemconfigure(self.image_item, image=self.photo)
        if self.dot:
            self.canvas.tag_raise(self.dot)
        self.root.after(50, self.update_mirror)  # ~20fps cho mirror, đủ dùng để test

    def poll_gaze(self):
        latest = None
        while not gaze_queue.empty():
            latest = gaze_queue.get()
        if latest:
            self.draw_dot(*latest)
        self.root.after(16, self.poll_gaze)  # ~60fps cho chấm, mượt hơn mirror

    def draw_dot(self, x_source, y_source):
        if not (math.isfinite(x_source) and math.isfinite(y_source)):
            if self.dot:
                self.canvas.itemconfigure(self.dot, state="hidden")
            return

        x = x_source / SOURCE_W * self.secondary.width
        y = y_source / SOURCE_H * self.secondary.height
        r = 12
        try:
            if self.dot:
                self.canvas.coords(self.dot, x - r, y - r, x + r, y + r)
                self.canvas.itemconfigure(self.dot, state="normal")
                self.canvas.tag_raise(self.dot)
            else:
                self.dot = self.canvas.create_oval(
                    x - r, y - r, x + r, y + r, fill="red", outline="white", width=2
                )
        except Exception as e:
            print("Lỗi vẽ chấm:", x, y, e)


if __name__ == "__main__":
    start_websocket_thread()
    primary, secondary = get_monitor_geometries()
    if not secondary:
        raise SystemExit("Cần ít nhất 2 màn hình để chạy test này.")
    root = tk.Tk()
    LiveOverlay(root, primary, secondary)
    root.mainloop()