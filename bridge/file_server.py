"""
Lets the browser play the finished videos.

The website is a web page, it cannot open files from the recordings folder. So the bridge also runs this tiny web server:
    http://127.0.0.1:8767/study_1/participant_16_ab12cd34/task_1/heatmap.mp4

Only for this computer (127.0.0.1), only .mp4 files, and only inside the recordings folder.
Range requests are supported: without them a <video> cannot jump to another moment.
"""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HOST = "127.0.0.1"
FILE_PORT = 8767
CHUNK = 256 * 1024


def make_handler(root):
    root = os.path.realpath(root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # no line per request in the terminal
            pass

        def _file(self):
            """The file this request asks for, or None when it is not allowed."""
            relative = unquote(urlparse(self.path).path).lstrip("/")
            full = os.path.realpath(os.path.join(root, relative))
            inside = full == root or full.startswith(root + os.sep)
            if inside and full.lower().endswith(".mp4") and os.path.isfile(full):
                return full
            return None

        def _send(self, with_body):
            full = self._file()
            if full is None:
                self.send_error(404)
                return
            size = os.path.getsize(full)
            start, end, status = 0, size - 1, 200

            requested = self.headers.get("Range", "")
            if requested.startswith("bytes=") and size > 0:
                first, _, last = requested[6:].partition("-")
                try:
                    if first == "":  # "-500" = the last 500 bytes
                        start = max(0, size - int(last))
                    else:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                except ValueError:
                    start, end = 0, size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206

            self.send_response(status)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-cache")  # a video that is made again must not come from the cache
            self.end_headers()
            if not with_body:
                return
            try:
                with open(full, "rb") as file:
                    file.seek(start)
                    left = end - start + 1
                    while left > 0:
                        data = file.read(min(CHUNK, left))
                        if not data:
                            break
                        self.wfile.write(data)
                        left -= len(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the browser stopped asking (it jumped to another moment): normal

        def do_GET(self):
            self._send(True)

        def do_HEAD(self):
            self._send(False)

    return Handler


def start(root, port=FILE_PORT):
    """Starts the server in the background (a thread). Returns the server."""
    server = ThreadingHTTPServer((HOST, port), make_handler(root))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
