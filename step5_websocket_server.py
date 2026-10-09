import asyncio, json
import websockets
import tobii_research as tr

SCREEN_W, SCREEN_H = 1920, 1080

async def stream_gaze(websocket):
    eyetracker = tr.find_all_eyetrackers()[0]
    loop = asyncio.get_event_loop()

    def gaze_data_callback(gaze_data):
        left = gaze_data["left_gaze_point_on_display_area"]
        right = gaze_data["right_gaze_point_on_display_area"]
        sample = {
            "timestamp": gaze_data["system_time_stamp"] / 1000.0,
            "x": ((left[0] + right[0]) / 2) * SCREEN_W,
            "y": ((left[1] + right[1]) / 2) * SCREEN_H,
            "participant_id": "p01",
            "session_id": "s01",
        }
        asyncio.run_coroutine_threadsafe(websocket.send(json.dumps(sample)), loop)

    eyetracker.subscribe_to(tr.EYETRACKER_GAZE_DATA, gaze_data_callback, as_dictionary=True)
    try:
        await asyncio.Future()
    finally:
        eyetracker.unsubscribe_from(tr.EYETRACKER_GAZE_DATA, gaze_data_callback)

async def main():
    async with websockets.serve(stream_gaze, "0.0.0.0", 8765):
        print("Server running at ws://0.0.0.0:8765 (Ctrl+C to stop)")
        await asyncio.Future()

asyncio.run(main())