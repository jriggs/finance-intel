"""
Pure helpers for assembling chat context and multimodal (vision) messages.

These are deliberately free of any module/global state (no model, RAG, or
network handles) so they can be unit-tested in isolation and reused by the chat
endpoint without dragging in the web/runtime layer.
"""
from __future__ import annotations

from pydantic import BaseModel


class Message(BaseModel):
    role: str  # "user" | "assistant" | "system"
    content: str


# ── Image utilities ────────────────────────────────────────────────────────────

def _image_format(image_b64: str) -> str:
    """Detect image format from the base64 header bytes."""
    if image_b64.startswith("iVBOR"):   return "png"
    if image_b64.startswith("R0lGOD"):  return "gif"
    if image_b64.startswith("UklGR"):   return "webp"
    return "jpeg"  # default / /9j/ JPEG header


def _build_vision_messages_for_description(image_b64: str, user_query: str) -> list:
    """Build an OpenAI-format message list for detailed image analysis."""
    fmt = _image_format(image_b64)
    image_url = f"data:image/{fmt};base64,{image_b64}"
    query_context = (
        f"\n\nPay particular attention to aspects relevant to: {user_query}"
        if user_query else ""
    )
    prompt = (
        "Analyze this image thoroughly and respond in these sections:\n\n"
        "OCR / TEXT: Extract every piece of visible text exactly as written — "
        "signs, labels, numbers, tables, captions, watermarks, UI elements, anything.\n\n"
        "OBJECTS & PEOPLE: Describe all visible objects, people, animals and their positions.\n\n"
        "SCENE: Colors, lighting, setting, and overall context.\n\n"
        "DETAILS: Actions, expressions, or any other notable information."
        + query_context
    )
    return [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": image_url}},
        {"type": "text", "text": prompt},
    ]}]


def _build_vision_messages(system: str, messages: list[Message], image_b64: str) -> list:
    """
    Build OpenAI-format messages for multimodal inference.
    The image is attached to the last user message.
    """
    fmt = _image_format(image_b64)
    image_url = f"data:image/{fmt};base64,{image_b64}"

    result = []
    if system:
        result.append({"role": "system", "content": system})

    for i, msg in enumerate(messages):
        if msg.role == "user" and i == len(messages) - 1:
            # Attach image to the final user message
            result.append({
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": msg.content},
                ],
            })
        else:
            result.append({"role": msg.role, "content": msg.content})
    return result


# ── Context assembly ───────────────────────────────────────────────────────────

def _assemble_system_prompt(
    base: str,
    rag_context: str,
    web_context: str,
) -> str:
    """Append RAG and web-search context blocks to the base system prompt."""
    system = base
    if rag_context:
        system += f"\n\n--- Relevant context from knowledge base ---\n{rag_context}\n--- End context ---"
    if web_context:
        system += (
            f"\n\nYou have already performed a live web search for the user's query. "
            f"The results are below. Use them to answer — do NOT say you cannot browse the internet.\n\n"
            f"--- Web search results ---\n{web_context}\n--- End search results ---"
        )
    return system


def _inject_into_last_user_message(
    messages: list[Message],
    url_context: str,
    image_description: str,
) -> list[Message]:
    """
    Inject fetched URL content and image description directly into the last
    user message. Placing context in the user turn (rather than only in the
    system prompt) ensures the model treats it as ground truth.
    Returns a new list; the original is not mutated.
    """
    result = list(messages)
    for i in range(len(result) - 1, -1, -1):
        if result[i].role == "user":
            extra = ""
            if url_context:
                extra += (
                    f"\n\n[The following content was fetched live from the URL you mentioned"
                    f" — use it to answer]\n{url_context}"
                )
            if image_description:
                extra += f"\n\n[Image content: {image_description}]"
            if extra:
                result[i] = Message(role="user", content=result[i].content + extra)
            break
    return result
