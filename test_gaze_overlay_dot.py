import asyncio
import json
import math
import threading
import queue
import tkinter as tk
import websockets

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
                    print("Capture:", data["x"], data["y"])
                    gaze_queue.put((data["x"], data["y"]))
        except Exception as e:
            print("Mất kết nối, thử lại sau 2 giây:", e)
            await asyncio.sleep(2)

def start_websocket_thread():
    threading.Thread(target=lambda: asyncio.run(listen()), daemon=True).start()

class GazeOverlay:
    def __init__(self, root):
        self.root = root
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="black")
        self.canvas = tk.Canvas(root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.root.bind("<Escape>", lambda e: root.destroy())
        self.dot = None
        self.poll()

    def poll(self):
        latest = None
        while not gaze_queue.empty():
            latest = gaze_queue.get()
        if latest:
            self.draw_dot(*latest)
        self.root.after(16, self.poll)  # ~60fps

    def draw_dot(self, x_source, y_source):
        if not (math.isfinite(x_source) and math.isfinite(y_source)):
            # Mất dấu mắt lúc này -> ẩn chấm đi thay vì crash
            if self.dot:
                self.canvas.itemconfigure(self.dot, state="hidden")
            return

        win_w = self.canvas.winfo_width()
        win_h = self.canvas.winfo_height()
        x = x_source / SOURCE_W * win_w
        y = y_source / SOURCE_H * win_h
        r = 12
        try:
            if self.dot:
                self.canvas.coords(self.dot, x - r, y - r, x + r, y + r)
                self.canvas.itemconfigure(self.dot, state="normal")
            else:
                self.dot = self.canvas.create_oval(
                    x - r, y - r, x + r, y + r, fill="red", outline="white", width=2
                )
        except Exception as e:
            print("Lỗi vẽ chấm:", x, y, e)

if __name__ == "__main__":
    start_websocket_thread()
    root = tk.Tk()
    GazeOverlay(root)
    root.mainloop()