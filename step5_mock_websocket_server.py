"""Step 5 (practice, no hardware needed): stream a simulated live gaze point
over a WebSocket, in the exact JSON shape the real Tobii version (and later
the website) will use.

Run it, then in a SECOND terminal (same venv activated) run test_client.py
to see the stream. Stop with Ctrl+C.
"""
import asyncio
import json
import math
import time

SCREEN_W, SCREEN_H = 1920, 1080
HOST, PORT = "localhost", 8765


async def stream_gaze(websocket):
    print("Client connected - streaming simulated gaze.")
    start = time.time()
    try:
        while True:
            elapsed = time.time() - start
            sample = {
                "timestamp": elapsed * 1000,                                 # ms since this connection started
                "x": SCREEN_W / 2 + 300 * math.sin(elapsed),                  # fake eye movement, left-right
                "y": SCREEN_H / 2 + 200 * math.sin(elapsed * 1.4 + 1),        # fake eye movement, up-down
                "participant_id": "mock01",
                "session_id": "mock_session",
            }
            await websocket.send(json.dumps(sample))
            await asyncio.sleep(1 / 60)   # ~60 samples/s, close to a real tracker's rate
    except Exception:
        # client disconnected (e.g. test_client.py finished its 20 samples) - that's normal
        pass
    finally:
        print("Client disconnected.")


async def main():
    import websockets

    async with websockets.serve(stream_gaze, HOST, PORT):
        print(f"Mock server dang chay tai ws://{HOST}:{PORT} (Ctrl+C de dung)")
        await asyncio.Future()   # run forever


if __name__ == "__main__":
    asyncio.run(main())
