"""Tests for main.py utility functions (no server/model required)."""

import os
import sys
import types
from pathlib import Path

# ── Isolated import of main.py ─────────────────────────────────────────────────
# main.py has heavy dependencies (llama-cpp-python, chromadb, sentence-transformers).
# We stub them out, import only the pure helpers we want to test, then restore
# sys.modules so the stubs don't leak into other test files.

os.environ.setdefault("MODEL_PATH", "")

# Stub the high-level modules that main.py imports directly.
# We stub at the module level (llm, rag, scraper, search, trainer) rather than
# their transitive dependencies so llm.py's ImportError guard never fires.
_llm_stub = types.ModuleType("llm")
_llm_stub.LLMEngine = object
_llm_stub.find_mmproj = lambda *a, **kw: None

_rag_stub = types.ModuleType("rag")
_rag_stub.RAGPipeline = lambda: None

_scraper_stub = types.ModuleType("scraper")
_scraper_stub.WebScraper = lambda: None

_search_stub = types.ModuleType("search")
_search_stub.search = None
_search_stub.search_with_urls = None
_search_stub.needs_search = lambda *a: False
_search_stub.extract_urls = lambda *a: []
_search_stub.close_http_client = lambda *a: None
_search_stub.shutdown_executor = lambda *a: None

_activity_stub = types.ModuleType("activity")
_activity_stub.emit = lambda *a, **kw: None
_activity_stub.subscribe = lambda: None
_activity_stub.unsubscribe = lambda *a: None

_trainer_stub = types.ModuleType("trainer")
for _fn in ("get_status","get_log","list_datasets","list_adapters",
            "start_training","stop_training","save_dataset_file"):
    setattr(_trainer_stub, _fn, lambda *a, **kw: {})

_dotenv_stub = types.ModuleType("dotenv")
_dotenv_stub.load_dotenv = lambda: None

_STUBS = {
    "llm":      _llm_stub,
    "rag":      _rag_stub,
    "scraper":  _scraper_stub,
    "search":   _search_stub,
    "activity": _activity_stub,
    "trainer":  _trainer_stub,
    "dotenv":   _dotenv_stub,
}

# Add backend/ to path
_backend = Path(__file__).parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

# Evict main/prompts so they re-import fresh with our stubs in place
for _mod in ("main", "prompts"):
    sys.modules.pop(_mod, None)

# Save any existing versions of the stub keys, then inject stubs
_saved_modules = {k: sys.modules[k] for k in _STUBS if k in sys.modules}
sys.modules.update(_STUBS)

try:
    from main import (
        Message,
        _assemble_system_prompt,
        _build_vision_messages,
        _image_format,
        _inject_into_last_user_message,
    )
finally:
    # Restore sys.modules — remove stubs, put back any originals
    for k in _STUBS:
        if k in _saved_modules:
            sys.modules[k] = _saved_modules[k]
        else:
            sys.modules.pop(k, None)
    # Also evict our modules so later tests re-import from disk cleanly
    for _mod in ("main", "llm", "rag", "scraper", "trainer", "activity"):
        sys.modules.pop(_mod, None)



# ── _image_format ──────────────────────────────────────────────────────────────

class TestImageFormat:
    def test_png_header(self):
        assert _image_format("iVBORw0KGgo=") == "png"

    def test_gif_header(self):
        assert _image_format("R0lGODlh") == "gif"

    def test_webp_header(self):
        assert _image_format("UklGRg==") == "webp"

    def test_jpeg_slash(self):
        assert _image_format("/9j/4AAQ") == "jpeg"

    def test_unknown_defaults_to_jpeg(self):
        assert _image_format("AAAAA") == "jpeg"


# ── _assemble_system_prompt ────────────────────────────────────────────────────

class TestAssembleSystemPrompt:
    def test_base_only(self):
        result = _assemble_system_prompt("You are helpful.", "", "")
        assert result == "You are helpful."

    def test_rag_context_appended(self):
        result = _assemble_system_prompt("Base.", "Some RAG data.", "")
        assert "knowledge base" in result
        assert "Some RAG data." in result

    def test_web_context_appended(self):
        result = _assemble_system_prompt("Base.", "", "Search results here.")
        assert "web search" in result.lower()
        assert "Search results here." in result

    def test_both_contexts_present(self):
        result = _assemble_system_prompt("Base.", "RAG.", "WEB.")
        assert "RAG." in result
        assert "WEB." in result
        assert result.startswith("Base.")

    def test_empty_rag_not_appended(self):
        result = _assemble_system_prompt("Base.", "", "")
        assert "knowledge base" not in result

    def test_empty_web_not_appended(self):
        result = _assemble_system_prompt("Base.", "", "")
        assert "web search" not in result.lower()


# ── _inject_into_last_user_message ────────────────────────────────────────────

class TestInjectIntoLastUserMessage:
    def _msgs(self):
        return [
            Message(role="user",      content="Hello"),
            Message(role="assistant", content="Hi!"),
            Message(role="user",      content="What is on that page?"),
        ]

    def test_url_context_injected_into_last_user(self):
        msgs = _inject_into_last_user_message(self._msgs(), "PAGE CONTENT", "")
        last_user = next(m for m in reversed(msgs) if m.role == "user")
        assert "PAGE CONTENT" in last_user.content
        assert "What is on that page?" in last_user.content

    def test_image_description_injected_into_last_user(self):
        msgs = _inject_into_last_user_message(self._msgs(), "", "A red cat.")
        last_user = next(m for m in reversed(msgs) if m.role == "user")
        assert "A red cat." in last_user.content

    def test_both_injected(self):
        msgs = _inject_into_last_user_message(self._msgs(), "PAGE", "IMAGE")
        last_user = next(m for m in reversed(msgs) if m.role == "user")
        assert "PAGE" in last_user.content
        assert "IMAGE" in last_user.content

    def test_earlier_user_messages_untouched(self):
        msgs = _inject_into_last_user_message(self._msgs(), "PAGE", "")
        assert msgs[0].content == "Hello"

    def test_no_injection_when_empty(self):
        original = self._msgs()
        result = _inject_into_last_user_message(original, "", "")
        for orig, new in zip(original, result):
            assert orig.content == new.content

    def test_original_list_not_mutated(self):
        original = self._msgs()
        original_contents = [m.content for m in original]
        _inject_into_last_user_message(original, "INJECTED", "")
        assert [m.content for m in original] == original_contents


# ── _build_vision_messages ─────────────────────────────────────────────────────

class TestBuildVisionMessages:
    def _msgs(self):
        return [Message(role="user", content="What is this?")]

    def test_system_included(self):
        result = _build_vision_messages("You are a vision model.", self._msgs(), "iVBORtest")
        roles = [m["role"] for m in result]
        assert "system" in roles

    def test_image_attached_to_last_user_message(self):
        result = _build_vision_messages("Sys", self._msgs(), "iVBORtest")
        last_user = next(m for m in reversed(result) if m["role"] == "user")
        content = last_user["content"]
        assert isinstance(content, list)
        types_in_content = [c["type"] for c in content]
        assert "image_url" in types_in_content
        assert "text" in types_in_content

    def test_png_format_detected(self):
        result = _build_vision_messages("Sys", self._msgs(), "iVBORtest")
        last_user = next(m for m in reversed(result) if m["role"] == "user")
        img_part = next(c for c in last_user["content"] if c["type"] == "image_url")
        assert "image/png" in img_part["image_url"]["url"]

    def test_jpeg_format_detected(self):
        result = _build_vision_messages("Sys", self._msgs(), "/9j/JPEG")
        last_user = next(m for m in reversed(result) if m["role"] == "user")
        img_part = next(c for c in last_user["content"] if c["type"] == "image_url")
        assert "image/jpeg" in img_part["image_url"]["url"]

    def test_earlier_messages_are_plain_string(self):
        msgs = [
            Message(role="user",      content="Earlier question"),
            Message(role="assistant", content="Earlier answer"),
            Message(role="user",      content="Now with image"),
        ]
        result = _build_vision_messages("Sys", msgs, "iVBORtest")
        first_user = next(m for m in result if m["role"] == "user")
        assert isinstance(first_user["content"], str)
