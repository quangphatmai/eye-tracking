"""Connects to a running gaze WebSocket server (mock or real - it doesn't
care which) and prints 20 samples, to confirm the stream works end to end.

Run step5_mock_websocket_server.py (or later step5_websocket_server.py) in
one terminal first, then run this in a second terminal with the same venv.
"""
import asyncio
import websockets

URL = "ws://localhost:8765"
N_SAMPLES = 20


async def listen():
    async with websockets.connect(URL) as ws:
        print(f"Connected to {URL} - printing {N_SAMPLES} samples:\n")
        for _ in range(N_SAMPLES):
            print(await ws.recv())


if __name__ == "__main__":
    asyncio.run(listen())
