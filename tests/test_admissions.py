"""Durable admission identity, FIFO and execution ownership contracts."""

import asyncio

import pytest

from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.executions import ExecutionLedger
from deep_harness.infrastructure.receipts import ReceiptLedger
from deep_harness.infrastructure.store import BusinessStore


@pytest.fixture
async def store(tmp_path):
    value = BusinessStore(tmp_path / "profile", "p1")
    await value.open()
    yield value
    await value.close()


async def session(store):
    return await store.create_session(
        principal="alice",
        request_id="create-1",
        prompt="Frozen",
        tools=("read",),
    )


async def test_retries_keep_one_admission_but_equal_text_with_new_id_is_distinct(store):
    chat = await session(store)
    first, retry = await asyncio.gather(
        *[
            store.admit(chat, principal="alice", submission_id="send-1", text="same")
            for _ in range(2)
        ]
    )
    assert first.id == retry.id
    second = await store.admit(
        chat,
        principal="alice",
        submission_id="send-2",
        text="same",
    )
    assert second.id != first.id
    assert len(await store.history(chat.id)) == 2
    with pytest.raises(DomainError, match="payload changed"):
        await store.admit(chat, principal="alice", submission_id="send-1", text="different")


async def test_creation_retry_is_bound_to_policy_and_principal(store):
    original = await session(store)
    assert (await session(store)).id == original.id
    with pytest.raises(DomainError, match="parameters changed"):
        await store.create_session(
            principal="alice",
            request_id="create-1",
            prompt="Changed",
            tools=("read",),
        )
    with pytest.raises(DomainError, match="not accessible"):
        await store.get_session(original.id, "bob")


async def test_claims_are_serial_and_unknown_execution_blocks_followers(store):
    chat = await session(store)
    first = await store.admit(chat, principal="alice", submission_id="1", text="one")
    second = await store.admit(chat, principal="alice", submission_id="2", text="two")
    ledger = ExecutionLedger(store)
    claims = await asyncio.gather(
        ledger.claim(chat.id, "owner-a"), ledger.claim(chat.id, "owner-b")
    )
    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    claim = claimed[0]
    assert claim.admission.id == first.id
    await ledger.recover()
    assert (await store.get_admission(first.id, chat.id)).status == "unknown"
    assert (await store.get_admission(second.id, chat.id)).status == "queued"
    assert await ledger.claim(chat.id, "new-owner") is None
    with pytest.raises(DomainError, match="no longer owns"):
        await ledger.settle(claim, "completed", "stale")


async def test_dispatched_effect_cannot_be_replayed_without_receipt(store):
    chat = await session(store)
    await store.admit(chat, principal="alice", submission_id="1", text="write")
    ledger = ExecutionLedger(store)
    claim = await ledger.claim(chat.id, "owner")
    receipts = ReceiptLedger(store, ledger)
    assert await receipts.prepare_effect(claim, "effect-1", "digest") is None
    await receipts.dispatch_effect(claim, "effect-1")
    with pytest.raises(DomainError, match="reconciled"):
        await receipts.prepare_effect(claim, "effect-1", "digest")


async def test_completed_effect_reuses_exact_result_and_refuses_changed_arguments(store):
    chat = await session(store)
    await store.admit(chat, principal="alice", submission_id="1", text="read")
    ledger = ExecutionLedger(store)
    claim = await ledger.claim(chat.id, "owner")
    receipts = ReceiptLedger(store, ledger)
    await receipts.prepare_effect(claim, "effect-1", "digest")
    await receipts.dispatch_effect(claim, "effect-1")
    await receipts.save_effect(claim, "effect-1", "original result")
    assert (await receipts.prepare_effect(claim, "effect-1", "digest"))[
        "result"
    ] == "original result"
    with pytest.raises(DomainError, match="arguments changed"):
        await receipts.prepare_effect(claim, "effect-1", "changed")
