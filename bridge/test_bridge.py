"""
Plays the part of the website, to check that bridge_server.py works.

1. Terminal 1:  python bridge_server.py --mode mock          (add --speed 10 to make it faster)
2. Terminal 2:  python test_bridge.py

It sends the same messages the website will send and prints what comes back.
"""

import asyncio
import json
import sys

import websockets

URL = "ws://127.0.0.1:8766"


async def receive(ws, wanted, timeout=30):
    """Wait for a message of type `wanted`, print everything that arrives on the way."""
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        message = json.loads(raw)
        print("   <-", message)
        if message["type"] == wanted:
            return message


async def send(ws, **message):
    print("->", message)
    await ws.send(json.dumps(message))


def check(condition, text):
    print(("   OK   " if condition else "   FAIL ") + text)
    if not condition:
        sys.exit(1)


async def main():
    async with websockets.connect(URL) as ws:
        ready = await receive(ws, "ready")
        check(ready["mode"] == "mock", "bridge says it is in mock mode")

        print("\n1. A task before the session must be refused")
        await send(ws, type="start_task", index=0, text="Find a hotel", url="https://www.booking.com", duration=60)
        await receive(ws, "error")
        check(True, "got an error, the connection stays open")

        print("\n2. Session + calibration")
        await send(ws, type="start_session", studyId=1, participantId=1)
        await receive(ws, "session_started")
        await send(ws, type="start_calibration")
        done = await receive(ws, "calibration_done")
        check(done["quality"] == "good", "calibration finished")

        print("\n3. Familiarization: the participant explores, then reads the intro and presses Next")
        await send(ws, type="start_familiarization", url="https://www.booking.com", duration=60)
        started = await receive(ws, "familiarization_started")
        check(started["duration"] == 60, "familiarization of 60 seconds")
        await asyncio.sleep(1)
        await send(ws, type="finish_task")  # the observer skips the waiting (so this test does not take a minute)
        await receive(ws, "intro_shown")
        await receive(ws, "familiarization_done")
        check(True, "intro shown, then the participant pressed Next")

        print("\n4. Task 1: the participant presses Start Task, the observer presses next after 1 second, duration capped at 180")
        await send(ws, type="start_task", index=0, text="Find a hotel", url="https://www.booking.com", duration=999)
        await receive(ws, "task_ready")
        started = await receive(ws, "task_started")
        check(started["duration"] == 180, "duration 999 was capped to 180 seconds")
        await asyncio.sleep(1)
        await send(ws, type="finish_task")
        finished = await receive(ws, "task_finished")
        check(finished["reason"] == "next", "task stopped early (reason: next)")

        print("\n5. Task 2: nobody presses anything, the 10 second limit ends it")
        await send(ws, type="start_task", index=1, text="Book a room", url="https://www.booking.com", duration=10)
        await receive(ws, "task_ready")
        await receive(ws, "task_started")
        finished = await receive(ws, "task_finished")
        check(finished["reason"] == "timeout", "task ended by itself (reason: timeout)")

        print("\n6. End of the session: the recordings are saved, no videos yet")
        await send(ws, type="end_session")
        ended = await receive(ws, "session_ended")
        check(ended["tasks"] == 2 and ended["participantId"] == 1, "session closed, 2 recorded tasks")

        print("\n7. Later, when nobody is being recorded: the videos, one participant after the other")
        await send(ws, type="process_participants", studyId=1, participants=[
            {"participantId": 1, "sessionId": ended["sessionId"], "tasks": 2},
            {"participantId": 2, "sessionId": "abcd1234", "tasks": 1},
        ])
        first = await receive(ws, "participant_done")
        check(first["participantId"] == 1 and len(first["videos"]) == 2, "participant 1 is ready")
        second = await receive(ws, "participant_done")
        check(second["participantId"] == 2, "participant 2 is ready")
        await receive(ws, "processing_done")

    print("\nAll good: the bridge works.")


try:
    asyncio.run(main())
except (ConnectionRefusedError, OSError):
    print("Could not connect. Is bridge_server.py running in another terminal?")
