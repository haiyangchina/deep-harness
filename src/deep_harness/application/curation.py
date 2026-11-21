"""Explicit AI curation of completed user turns into reviewable candidates."""

import asyncio
import json
import tomllib
from pathlib import Path

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.curator import CuratorLedger
from deep_harness.server_config import number

CURATOR_PROMPT = (
    "Extract at most five stable facts or preferences explicitly stated by the user. "
    "Never infer facts, include transient tasks, or include passwords, tokens, secrets or keys. "
    "Return only a JSON array of short strings. Return [] when there is nothing durable."
)


class CurationService:
    def __init__(self, store, model):
        self.store = store
        self.model = model
        self.ledger = CuratorLedger(store)
        self.lock = asyncio.Lock()

    async def propose(self, session_id):
        async with self.lock:
            return await self._propose(session_id)

    async def _propose(self, session_id):
        texts = await self.ledger.completed_user_texts(session_id)
        if not texts:
            raise DomainError("curation_empty", "No completed user turns are available")
        retained = []
        size = 0
        for text in reversed(texts):
            if size + len(text) > number("curation.model_input_chars"):
                break
            retained.append(text)
            size += len(text)
        retained.reverse()
        history_hash = fingerprint(retained)
        model_hash = fingerprint(self.model.identity)
        run_id = fingerprint({"session": session_id, "history": history_hash, "model": model_hash})
        existing = await self.ledger.get_run(run_id)
        if existing is not None:
            return existing
        message = {
            "id": f"{run_id}:input",
            "role": "user",
            "content": json.dumps(retained, ensure_ascii=False),
        }
        async with asyncio.timeout(self.model.request_timeout):
            response = await self.model.complete(CURATOR_PROMPT, [message], ())
        contents = parse_candidates(response.get("content"))
        return await self.ledger.save(
            run_id, session_id, history_hash, model_hash, contents, response.get("usage")
        )


def auto_curation_enabled(profile_home: Path) -> bool:
    path = profile_home / "curation.toml"
    if not path.exists():
        return False
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DomainError("curation_config", "Curator configuration cannot be read") from exc
    if config not in ({"auto": True}, {"auto": False}):
        raise DomainError("curation_config", "curation.toml must contain only auto = true/false")
    return config["auto"]


def parse_candidates(content):
    if not isinstance(content, str) or len(content) > number("curation.model_output_chars"):
        raise DomainError("curation_response", "Curator response is not a bounded JSON string")
    value = content.strip()
    if value.startswith("```json") and value.endswith("```"):
        value = value[7:-3].strip()
    try:
        candidates = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DomainError("curation_response", "Curator did not return valid JSON") from exc
    if not isinstance(candidates, list) or len(candidates) > number("curation.max_candidates"):
        raise DomainError("curation_response", "Curator must return at most five candidates")
    result = []
    for item in candidates:
        if not isinstance(item, str) or not 1 <= len(item.strip()) <= number(
            "memory.content_max_length"
        ):
            raise DomainError("curation_response", "Curator candidate content is invalid")
        normalized = item.strip()
        if normalized not in result:
            result.append(normalized)
    return result
