"""Cursor-based SSE delivery from the committed journal, with reconnect replay."""

import json


async def stream_events(store, session_id, after, disconnected):
    cursor = after
    while not await disconnected():
        # Capture before reading: a concurrent commit cannot be lost between read and wait.
        revision = store.revision
        events = await store.event_history(session_id, cursor)
        if events:
            for event in events:
                cursor = event["seq"]
                payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {payload}\n\n"
        else:
            yield ": keep-alive\n\n"
            await store.wait_for_change(revision)
