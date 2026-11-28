"""Delete graph threads belonging to a retired session."""

from deep_harness.domain.identity import fingerprint


async def delete_session_threads(saver, session_id, task_ids):
    threads = {session_id}
    # Finish enumeration before deleting. The saver holds its lock while listing.
    async for item in saver.alist(None):
        thread = item.config["configurable"]["thread_id"]
        if thread.startswith((session_id + ":delegate:", session_id + ":verify:",
                              "delivery:" + session_id + ":")):
            threads.add(thread)
        elif thread.startswith("delivery:"):
            receipt = item.checkpoint["channel_values"].get("receipt")
            if not isinstance(receipt, dict) or not receipt.get("revision"):
                continue
            for owner in task_ids:
                for action, bundle in (
                    ("apply", receipt.get("change_set_id")), ("undo", None),
                ):
                    key = fingerprint([owner, action, receipt["revision"], bundle])
                    if thread == "delivery:" + key:
                        threads.add(thread)
    for thread in threads:
        await saver.adelete_thread(thread)
