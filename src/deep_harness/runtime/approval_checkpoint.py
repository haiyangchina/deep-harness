"""Translate checkpoint interrupts into business approval IDs and resume commands."""

from langgraph.types import Command

from deep_harness.domain.models import DomainError


def checkpoint_interrupts(state):
    return tuple(item for task in state.tasks for item in task.interrupts)


def approval_ids(interrupts, generation):
    ids = []
    for item in interrupts:
        value = item.value
        if not isinstance(value, dict) or value.get("generation") != generation:
            raise DomainError("approval_missing", "Checkpoint approval generation is inconsistent")
        current = value.get("interaction_ids", (value.get("id"),))
        if not current or any(not isinstance(key, str) for key in current):
            raise DomainError("approval_missing", "Checkpoint has no verifiable approval ID")
        ids.extend(current)
    return tuple(dict.fromkeys(ids))


def resume_command(state):
    interrupts = checkpoint_interrupts(state)
    if not interrupts:
        raise DomainError("approval_missing", "Checkpoint has no pending approval")
    return Command(resume={item.id: True for item in interrupts})
