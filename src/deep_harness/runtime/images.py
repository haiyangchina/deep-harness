"""Resolve owned image references only at the model request boundary."""

from base64 import b64encode

from deep_harness.domain.images import MAX_IMAGE_BYTES, MAX_IMAGES
from deep_harness.domain.models import DomainError

# A full current message always fits. Base64 adds at most one third to this budget.
IMAGE_REQUEST_BYTES = MAX_IMAGES * MAX_IMAGE_BYTES
OMITTED = "[Earlier images omitted from the model context.]"


async def user_message(owner):
    admission = owner.claim.admission
    message = {"id": f"{admission.id}:user", "role": "user", "content": admission.text}
    if owner.artifacts is not None:
        if image_ids := await owner.store.admission_image_ids(admission):
            message["image_ids"] = image_ids
    return message


async def image_content(artifacts, session_id, turns):
    """Map message indexes to transient model content, favoring recent image turns."""
    remaining = IMAGE_REQUEST_BYTES
    content = {}
    encoded = {}
    for index, text, ids in reversed(turns):
        if artifacts is None:
            raise DomainError("artifact_not_found", "Images are unavailable")
        owned = [await artifacts.image(session_id, image_id) for image_id in ids]
        size = sum(row["size"] for row, _ in owned)
        if size > remaining:
            content[index] = f"{text}\n\n{OMITTED}" if text else OMITTED
            continue
        remaining -= size
        prompt = text or ("请分析这张图片。" if len(ids) == 1 else "请分析这些图片。")
        blocks = [{"type": "text", "text": prompt}]
        for row, path in owned:
            artifact_id = row["id"]
            if artifact_id not in encoded:
                data = await artifacts.image_bytes(row, path)
                encoded[artifact_id] = b64encode(data).decode("ascii")
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{row['kind']};base64,{encoded[artifact_id]}"},
                }
            )
        content[index] = blocks
    return content


def included_ids(turns, content):
    return list(
        dict.fromkeys(
            image_id
            for index, _, ids in turns
            if isinstance(content[index], list)
            for image_id in ids
        )
    )
