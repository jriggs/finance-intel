"""
Full-stack Chat LLM App — FastAPI Backend
Supports: local GGUF model inference, streaming SSE, RAG with ChromaDB
"""

from __future__ import annotations

# ── Package path wiring ───────────────────────────────────────────────────────
# Adds core/, services/, ai/, routers/ to sys.path so all modules can be
# imported by their bare name (e.g. `import signals`, `import db`) regardless
# of which subdirectory they live in.
import sys as _sys
from pathlib import Path as _Path
_here = _Path(__file__).parent
for _pkg in ("core", "services", "ai", "routers"):
    _p = str(_here / _pkg)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
del _sys, _Path, _here, _pkg, _p
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

# Must run before any local imports so module-level os.getenv() calls in
# rag.py, llm.py etc. pick up values from .env instead of falling back to defaults.
load_dotenv()

# Use locally cached HuggingFace models — skip update checks (HF often unreachable)
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# ── Logging ───────────────────────────────────────────────────────────────────
# Ring buffer, root config, and third-party noise suppression live in
# logging_config. _LOG_BUFFER is re-exported because finance_router (the
# /api/finance/logs endpoint) reads it as main._LOG_BUFFER.
from logging_config import LOG_BUFFER as _LOG_BUFFER, configure_logging

configure_logging()

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from activity import emit as _emit
from activity import subscribe as _activity_subscribe
from activity import unsubscribe as _activity_unsubscribe
from finance_router import init_finance, shutdown_finance
from finance_router import router as finance_router
from llm import LLMEngine, find_mmproj
from prompts import build_prompt, detect_template
from rag import RAGPipeline
from scraper import WebScraper
from search import (
    close_http_client as close_search_client,
)
from search import (
    extract_urls,
    needs_search,
)
from search import (
    search_with_urls as web_search_with_urls,
)
from search import (
    shutdown_executor as shutdown_search_executor,
)
from training_router import router as training_router
from chat_context import (
    Message,
    _assemble_system_prompt,
    _build_vision_messages,
    _build_vision_messages_for_description,
    _image_format,
    _inject_into_last_user_message,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global llm, active_template, vision_llm, _main_model_path, rag
    # Saved pref (last user-selected model) wins over the static env var.
    model_path = _load_model_pref() or os.getenv("MODEL_PATH", "")
    if model_path and Path(model_path).exists():
        print(f"Loading model from {model_path}...")
        llm = LLMEngine(model_path)
        active_template = detect_template(model_path)
        _main_model_path = model_path
        print(f"Model loaded. Template: {active_template}")
    else:
        print("[WARNING] No MODEL_PATH set or file not found. Set MODEL_PATH in .env")

    # If the main model itself supports vision, use it directly (no swap needed)
    if llm is not None and llm.is_multimodal:
        vision_llm = llm
        print("Vision: main model is multimodal — no swap needed.")
    else:
        vision_path = _find_vision_model_path()
        if vision_path:
            print(f"Vision: swap mode — will load {Path(vision_path).name} on demand.")

    # Start finance module immediately (rag=None until background load finishes)
    init_finance(llm, rag, scraper)

    # Load RAG in background — server is already accepting requests.
    # Once ready, inject it into the scheduler and seed the knowledge base.
    async def _load_rag_background():
        global rag
        loop = asyncio.get_running_loop()
        try:
            rag = await loop.run_in_executor(None, RAGPipeline)
            scheduler.set_rag(rag)
            print("RAG pipeline ready")
            await _seed_knowledge_base()
        except Exception as e:
            print(f"[WARNING] RAG pipeline failed to load: {e}")

    seed_task = asyncio.create_task(_load_rag_background())

    yield

    # ── Shutdown ───────────────────────────────────────────────────────────────
    # Cancel background seeding if still in progress
    seed_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await seed_task

    # Drain all remaining background tasks (URL indexing, etc.)
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)

    # Close HTTP clients and thread pools in dependency order
    shutdown_finance()
    await scraper.aclose()
    await close_search_client()
    shutdown_search_executor()
    if rag:
        rag.close()
    print("[Shutdown] All resources closed.")


app = FastAPI(title="Local Chat LLM API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Finance routes (all under /api/finance/*) ─────────────────────────────────
app.include_router(finance_router)
# ── Training routes (all under /api/train/*) ──────────────────────────────────
app.include_router(training_router)

# Serve the finance dashboard at /finance
from fastapi.responses import FileResponse as _FileResponse

_FINANCE_FRONTEND = Path(__file__).parent.parent / "frontend" / "index.html"

@app.get("/finance", response_class=_FileResponse)
async def finance_dashboard():
    return _FileResponse(str(_FINANCE_FRONTEND))

# ── Application state ──────────────────────────────────────────────────────────
llm: LLMEngine | None = None          # active text model
vision_llm: LLMEngine | None = None  # dedicated vision model (only if fits in RAM)
rag: RAGPipeline | None = None
scraper: WebScraper = WebScraper()
active_template: str = "mistral"   # tracks the prompt format for the loaded model
switching_model: bool = False       # lock during hot-swap
_main_model_path: str = ""          # remembered so we can reload after vision swap
# asyncio.Lock() is safe at module level on Python 3.10+ (no longer binds to a loop)
_swap_lock = asyncio.Lock()

# Tracks fire-and-forget background tasks so the GC doesn't destroy them mid-run
# and exceptions are surfaced on completion rather than silently dropped.
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    """Schedule a background coroutine, keeping a strong reference until it finishes."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

MODELS_DIR = Path(__file__).parent.parent / "models"

# ── Model preference persistence ───────────────────────────────────────────────
# Stored in finance.db (app_settings table) so it survives restarts.
# Preferred over MODEL_PATH in .env — .env is the "factory default", this is
# the user's last explicit choice.

def _save_model_pref(model_path: str) -> None:
    """Write the active model path to the DB after every successful switch."""
    try:
        from db import set_setting
        set_setting("model_path", model_path)
        print(f"[Pref] Saved model preference: {Path(model_path).name}")
    except Exception as e:
        print(f"[Pref] Could not save model preference: {e}")


def _load_model_pref() -> str:
    """Return the last explicitly chosen model path, or '' if none saved."""
    try:
        from db import get_setting
        path = get_setting("model_path", "")
        if path and Path(path).exists():
            return path
        if path:
            print(f"[Pref] Saved model not found at {path!r}, falling back to MODEL_PATH")
    except Exception as e:
        print(f"[Pref] Could not load model preference: {e}")
    return ""

# ── Seed knowledge base ────────────────────────────────────────────────────────
# Crawled once on first run (skipped if already indexed).
# Re-trigger any time via POST /api/seed, or retrain via the UI.
# max_pages is kept small — we want headlines/summaries, not full site crawls.

_SEED_SOURCES: list[dict] = [
    # ── General news ──────────────────────────────────────────────────────────
    {"url": "https://apnews.com/",               "name": "AP News",         "max_pages": 6},
    {"url": "https://www.bbc.com/news",          "name": "BBC News",        "max_pages": 6},
    {"url": "https://www.reuters.com/",          "name": "Reuters",         "max_pages": 6},
    {"url": "https://www.npr.org/sections/news/","name": "NPR News",        "max_pages": 5},
    {"url": "https://www.theguardian.com/world", "name": "The Guardian",    "max_pages": 5},
    {"url": "https://www.aljazeera.com/news/",   "name": "Al Jazeera",      "max_pages": 5},
    # ── Tech ──────────────────────────────────────────────────────────────────
    {"url": "https://techcrunch.com/",           "name": "TechCrunch",      "max_pages": 5},
    {"url": "https://arstechnica.com/",          "name": "Ars Technica",    "max_pages": 5},
    {"url": "https://www.theverge.com/",         "name": "The Verge",       "max_pages": 5},
    {"url": "https://news.ycombinator.com/",     "name": "Hacker News",     "max_pages": 3},
    # ── Finance ───────────────────────────────────────────────────────────────
    {"url": "https://www.marketwatch.com/",      "name": "MarketWatch",     "max_pages": 4},
    {"url": "https://www.cnbc.com/finance/",     "name": "CNBC Finance",    "max_pages": 4},
    # ── Science ───────────────────────────────────────────────────────────────
    {"url": "https://www.sciencedaily.com/",     "name": "ScienceDaily",    "max_pages": 5},
    {"url": "https://www.newscientist.com/",     "name": "New Scientist",   "max_pages": 4},
    # ── Reddit communities (use JSON API — no scraping issues) ────────────────
    {"url": "https://www.reddit.com/r/worldnews/",  "name": "r/worldnews",  "max_pages": 1},
    {"url": "https://www.reddit.com/r/technology/", "name": "r/technology", "max_pages": 1},
    {"url": "https://www.reddit.com/r/science/",    "name": "r/science",    "max_pages": 1},
    {"url": "https://www.reddit.com/r/economics/",  "name": "r/economics",  "max_pages": 1},
]


# ── Model utilities ────────────────────────────────────────────────────────────

def _find_vision_model_path() -> str | None:
    """Return the vision model path — explicit env var takes priority."""
    explicit = os.getenv("VISION_MODEL_PATH", "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    # Auto-detect: first vision-capable model in models dir
    MODELS_DIR.mkdir(exist_ok=True)
    for f in sorted(MODELS_DIR.glob("*.gguf")):
        if "mmproj" in f.name.lower() or "mm-proj" in f.name.lower():
            continue
        if find_mmproj(str(f)) is not None:
            return str(f)
    return None


def scan_models() -> list:
    """Scan the models directory for .gguf files."""
    MODELS_DIR.mkdir(exist_ok=True)
    models = []
    for f in sorted(MODELS_DIR.glob("*.gguf")):
        # Skip mmproj files — they're not standalone models
        if "mmproj" in f.name.lower() or "mm-proj" in f.name.lower():
            continue
        size_gb = round(f.stat().st_size / 1e9, 1)
        template = detect_template(str(f))
        is_active = llm is not None and Path(llm.model_path).resolve() == f.resolve()
        has_vision = find_mmproj(str(f)) is not None
        models.append({
            "name": f.name,
            "path": str(f),
            "size_gb": size_gb,
            "template": template,
            "active": is_active,
            "vision": has_vision,
        })
    return models


# ── Request / Response models ──────────────────────────────────────────────────
# Message and the pure chat-context/vision helpers live in chat_context.py.

class ChatRequest(BaseModel):
    messages: list[Message]
    use_rag: bool = True
    use_web_search: bool = False
    temperature: float = 0.7
    max_tokens: int = 1024
    system_prompt: str | None = None
    image: str | None = None   # base64-encoded image, any common format


class SourceRequest(BaseModel):
    url: str
    name: str | None = None


class SourceTextRequest(BaseModel):
    text: str
    name: str


class RetrainRequest(BaseModel):
    source_ids: list[str] | None = None  # None = retrain all


# ── Context assembly ───────────────────────────────────────────────────────────

async def _fetch_url_context(user_message: str) -> tuple[str, list[str]]:
    """
    Extract URLs from user_message, fetch each one, and return
    (formatted_context_string, list_of_urls_found).
    """
    urls = extract_urls(user_message)
    if not urls:
        return "", []

    parts = []
    for url in urls[:3]:  # cap at 3 URLs per message
        try:
            # Normalise malformed URLs (e.g. "https:example.com" → "https://example.com")
            if re.match(r"https?:[^/]", url):
                url = re.sub(r"(https?:)([^/])", r"\1//\2", url)
            elif re.match(r"https?:/[^/]", url):
                url = re.sub(r"(https?:)/([^/])", r"\1//\2", url)
            if url.lower().startswith("www."):
                url = "https://" + url

            print(f"[URL] Fetching: {url}")
            _emit("🌐", "Opening URL", url[:60])
            page = await scraper.fetch_single(url)
            if page and page.get("text"):
                title = page.get("title") or url
                _emit("📄", "Parsing page", title[:60])
                text  = page["text"][:3000]  # ~750 tokens per page
                parts.append(f"[Page: {title}]\nURL: {url}\n\n{text}")
                _emit("✅", "Page ready", f"{len(page['text'])} chars · {title[:40]}")
                print(f"[URL] Fetched {len(page['text'])} chars from {url}")
            else:
                _emit("⚠️", "No content", url[:60])
                print(f"[URL] No content extracted from {url}")
        except Exception as e:
            print(f"[URL] Failed to fetch {url}: {e}")

    return "\n\n---\n\n".join(parts), urls


# ── Vision swap ────────────────────────────────────────────────────────────────

async def _describe_image(image_b64: str, user_query: str) -> str:
    """
    Get an image description using the best available strategy:
    - If vision_llm is already loaded alongside the main model, use it directly.
    - Otherwise swap: unload main → load vision → describe → unload vision → reload main.
    """
    global llm, active_template, vision_llm, _main_model_path

    messages = _build_vision_messages_for_description(image_b64, user_query)

    # ── Fast path: dedicated vision model already in memory ───────────────────
    if vision_llm is not None and vision_llm.is_multimodal:
        description = ""
        async for token in vision_llm.stream_vision(messages, temperature=0.2, max_tokens=1024):
            description += token
        return description.strip()

    # ── Swap path: unload main model, run vision, reload main ─────────────────
    vision_path = _find_vision_model_path()
    if not vision_path:
        return ""

    async with _swap_lock:
        saved_path     = _main_model_path
        saved_template = active_template
        description    = ""
        loop           = asyncio.get_running_loop()
        try:
            print(f"[Vision] Unloading main model, loading {Path(vision_path).name}")
            old_engine = llm
            # Null out global before freeing so concurrent readers see None, not a freed engine
            llm        = None
            vision_llm = None
            await loop.run_in_executor(None, old_engine.free)
            del old_engine
            await asyncio.sleep(1.5)  # give Metal/CUDA time to release resources

            print("[Vision] Loading vision model...")
            v_engine = await loop.run_in_executor(None, LLMEngine, vision_path)
            print("[Vision] Vision model loaded, describing image...")
            async for token in v_engine.stream_vision(messages, temperature=0.2, max_tokens=1024):
                description += token
            description = description.strip()
            print(f"[Vision] Description ready ({len(description)} chars)")

            await loop.run_in_executor(None, v_engine.free)
            del v_engine
            await asyncio.sleep(1.5)

        except Exception as e:
            print(f"[Vision] Swap error: {type(e).__name__}: {e}")
        finally:
            # Always reload the main model
            if llm is None and saved_path:
                print("[Vision] Reloading main model...")
                try:
                    llm = await loop.run_in_executor(None, LLMEngine, saved_path)
                    active_template = saved_template
                    if llm.is_multimodal:
                        vision_llm = llm
                    print("[Vision] Main model ready.")
                except Exception as e:
                    print(f"[Vision] Failed to reload main model: {e}")

    return description


# ── Context window trimming ────────────────────────────────────────────────────

async def trim_messages_to_fit(
    system: str,
    messages: list[Message],
    template: str,
    n_ctx: int,
    max_tokens: int,
) -> list[Message]:
    """Drop oldest messages until the prompt fits within the context window.
    llm.tokenize() is a blocking C-extension call — runs in the default executor
    so it never stalls the event loop."""
    loop    = asyncio.get_running_loop()
    reserve = max_tokens + 64          # space for response + formatting overhead
    limit   = max(n_ctx - reserve, 64) # never collapse to nothing

    msgs = list(messages)
    while len(msgs) > 1:
        prompt      = build_prompt(system, msgs, template)
        token_count = await loop.run_in_executor(None, lambda p=prompt: len(llm.tokenize(p)))
        if token_count <= limit:
            break
        msgs = msgs[1:]  # drop oldest message and retry

    return msgs


# ── Chat Endpoint (streaming SSE) ─────────────────────────────────────────────

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if llm is None:
        raise HTTPException(503, "Model not loaded. Set MODEL_PATH in .env and restart.")

    last_user_msg = next(
        (m.content for m in reversed(req.messages) if m.role == "user"), ""
    )

    # For short follow-up messages ("and mens?", "what about X?"), build a
    # richer search query by prepending context from the previous user message.
    _prev_user_msgs = [m.content for m in req.messages if m.role == "user"]
    _search_query = last_user_msg
    _is_followup = (
        len(last_user_msg.split()) <= 8
        and len(_prev_user_msgs) >= 2
        and needs_search(_prev_user_msgs[-2])
    )
    if _is_followup:
        _search_query = _prev_user_msgs[-2] + " " + last_user_msg

    # Pre-check search eligibility — pure CPU, ~0 ms, so do it before launching tasks
    _should_search = (
        req.use_web_search
        and bool(last_user_msg)
        and (needs_search(last_user_msg) or _is_followup)
    )

    # Emit activity events for each concurrent I/O path before launching tasks
    if req.use_rag and last_user_msg:
        _emit("🧠", "Querying knowledge base")
    urls_preview = extract_urls(last_user_msg)
    if urls_preview:
        _emit("🌐", "Fetching page", urls_preview[0][:60])
    if _should_search:
        _emit("🔍", "Searching the web", last_user_msg[:60])

    # Launch all independent I/O concurrently instead of awaiting sequentially
    rag_task    = (
        asyncio.create_task(rag.query_async(last_user_msg, n_results=2))
        if req.use_rag and last_user_msg else None
    )
    url_task    = (
        asyncio.create_task(_fetch_url_context(last_user_msg))
        if last_user_msg else None
    )
    search_task = (
        asyncio.create_task(web_search_with_urls(_search_query, max_results=5))
        if _should_search else None
    )

    rag_context              = (await rag_task) if rag_task else ""
    url_context, urls_in_msg = (await url_task) if url_task else ("", [])

    if rag_context:
        _emit("✅", "Knowledge base ready", f"{len(rag_context)} chars retrieved")

    web_context  = ""
    result_urls: list[str] = []
    if search_task:
        if urls_in_msg:
            # URL found in message — cancel the web search and await cancellation
            search_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await search_task
        else:
            search_result = await search_task
            web_context, result_urls = search_result if search_result else ("", [])
            if not web_context:
                print("[Search] No results — model will answer from training data only")
            elif result_urls:
                _emit("✅", "Web search done", f"{len(result_urls)} sources found")
                # Background: fetch + index new sources so future queries benefit
                for result_url in result_urls[:4]:
                    _fire_and_forget(_index_url_if_new(result_url))
    elif req.use_web_search and last_user_msg and not _should_search:
        print("[Search] Skipped — query doesn't need real-time data")

    # Build system prompt with all context blocks attached
    base_system = req.system_prompt or os.getenv(
        "DEFAULT_SYSTEM_PROMPT",
        "You are a helpful, knowledgeable assistant. Answer clearly and concisely."
    )
    # Always prepend current UTC datetime so the model can answer time/date
    # questions directly without a web search. Also explicitly instruct it to
    # compute local times from this — otherwise models tend to deflect.
    _now = datetime.now(UTC)
    _datetime_line = (
        f"[Current UTC time: {_now.strftime('%A, %B %d, %Y %H:%M UTC')}. "
        f"Use this to answer time/date questions. "
        f"Never claim you cannot access the internet — if search results are provided use them, otherwise answer from training data. "
        f"Do not add unsolicited disclaimers, caveats, or suggestions to consult professionals at the end of responses. Answer the question and stop.]"
    )
    base_system = _datetime_line + "\n\n" + base_system
    system = _assemble_system_prompt(base_system, rag_context, web_context)

    # Image description — run now if vision_llm is already in memory
    image_description = ""
    if req.image and not llm.is_multimodal and vision_llm is not None:
        print("Describing image with vision model…")
        _emit("👁️", "Analyzing image")
        image_description = await _describe_image(req.image, last_user_msg)
        if image_description:
            _emit("✅", "Image analysis complete")
        print(f"Image description ({len(image_description)} chars) ready.")

    # Inject URL content + image description into the last user turn
    messages_to_use = _inject_into_last_user_message(
        req.messages, url_context, image_description
    )

    # Trim to fit context window
    n_ctx = llm.n_ctx()
    trimmed_messages = await trim_messages_to_fit(
        system, messages_to_use, active_template, n_ctx, req.max_tokens
    )

    async def token_stream() -> AsyncGenerator[str, None]:
        nonlocal image_description, trimmed_messages

        # Swap path: vision model not yet loaded — do it now with a status token
        if req.image and not llm.is_multimodal and not image_description:
            _analyzing_payload = json.dumps({"token": "🔍 *Analyzing image…*\n\n", "done": False})
            yield f"data: {_analyzing_payload}\n\n"
            _emit("👁️", "Analyzing image (loading vision model…)")
            image_description = await _describe_image(req.image, last_user_msg)
            if image_description:
                _emit("✅", "Image analysis complete")
            if image_description:
                yield f"data: {json.dumps({'image_description': image_description})}\n\n"
                msgs = _inject_into_last_user_message(req.messages, url_context, image_description)
                trimmed_messages = await trim_messages_to_fit(
                    system, msgs, active_template, llm.n_ctx(), req.max_tokens
                )

        if req.image and llm.is_multimodal:
            vision_messages = _build_vision_messages(system, trimmed_messages, req.image)
            gen = llm.stream_vision(vision_messages, req.temperature, req.max_tokens)
        else:
            full_prompt = build_prompt(system, trimmed_messages, active_template)
            gen = llm.stream(full_prompt, req.temperature, req.max_tokens)

        async for token in gen:
            yield f"data: {json.dumps({'token': token, 'done': False})}\n\n"
        yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"

    return StreamingResponse(
        token_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Sources / RAG Endpoints ────────────────────────────────────────────────────

@app.get("/api/sources")
async def list_sources():
    return {"sources": rag.list_sources()}


@app.post("/api/sources/url")
async def add_url_source(req: SourceRequest, background_tasks: BackgroundTasks):
    """Crawl a URL and add its content to the knowledge base."""
    source_id = rag.create_source(
        source_id=req.url,
        name=req.name or req.url,
        source_type="url",
    )
    background_tasks.add_task(_crawl_and_index, req.url, source_id)
    return {"source_id": source_id, "status": "indexing"}


@app.post("/api/sources/text")
async def add_text_source(req: SourceTextRequest):
    """Add raw text directly to the knowledge base."""
    if len(req.text.encode()) > 20 * 1024 * 1024:
        raise HTTPException(413, "Text exceeds 20MB limit")
    source_id = await rag.add_text_async(req.text, name=req.name)
    return {"source_id": source_id, "status": "indexed"}


@app.delete("/api/sources/{source_id:path}")
async def delete_source(source_id: str):
    rag.delete_source(source_id)
    return {"deleted": source_id}


@app.post("/api/sources/retrain")
async def retrain(req: RetrainRequest, background_tasks: BackgroundTasks):
    """Re-crawl and re-index URL sources."""
    background_tasks.add_task(_retrain_sources, req.source_ids)
    return {"status": "retraining started"}


class SeedRequest(BaseModel):
    force: bool = False  # True = re-index even if source already exists


@app.post("/api/seed")
async def seed_sources(req: SeedRequest, background_tasks: BackgroundTasks):
    """
    Index the built-in seed sources into the knowledge base.
    By default skips sources already indexed — pass force=true to re-crawl all.
    """
    background_tasks.add_task(_seed_knowledge_base, req.force)
    new_count = sum(1 for s in _SEED_SOURCES if req.force or not rag.has_source(s["url"]))
    return {
        "status": "seeding started",
        "sources": new_count,
        "force": req.force,
    }


@app.get("/api/seed")
async def seed_status():
    """List seed sources and their current index status."""
    return {
        "sources": [
            {
                "url":     s["url"],
                "name":    s["name"],
                "indexed": rag.has_source(s["url"]),
                "status":  rag.get_source_status(s["url"]).get("status", "not indexed"),
            }
            for s in _SEED_SOURCES
        ]
    }


@app.get("/api/sources/{source_id:path}/status")
async def source_status(source_id: str):
    return rag.get_source_status(source_id)


# ── Model Management ───────────────────────────────────────────────────────────

class SwitchModelRequest(BaseModel):
    path: str   # full path or just filename (relative to models/)


@app.get("/api/models")
async def list_models():
    """List all .gguf files in the models directory."""
    return {"models": scan_models(), "switching": switching_model}


@app.get("/api/model")
async def model_info():
    """Current model info (kept for backwards compat with frontend status bar)."""
    if llm is None:
        return {"loaded": False, "path": os.getenv("MODEL_PATH", ""), "switching": switching_model}
    return {
        "loaded":    True,
        "path":      llm.model_path,
        "name":      Path(llm.model_path).name,
        "template":  active_template,
        "params":    llm.params(),
        "switching": switching_model,
    }


@app.post("/api/models/switch")
async def switch_model(req: SwitchModelRequest, background_tasks: BackgroundTasks):
    """Hot-swap to a different model. Unloads current, loads new one."""
    global switching_model

    # Resolve path — accept full path or just filename
    target = Path(req.path)
    if not target.is_absolute():
        target = MODELS_DIR / target

    if not target.exists():
        raise HTTPException(404, f"Model file not found: {target}")

    if llm is not None and Path(llm.model_path).resolve() == target.resolve():
        return {"status": "already_loaded", "name": target.name}

    if switching_model:
        raise HTTPException(409, "Already switching models, please wait.")

    switching_model = True
    background_tasks.add_task(_do_switch, str(target))
    return {"status": "switching", "name": target.name}


async def _do_switch(model_path: str):
    global llm, active_template, switching_model, _main_model_path, vision_llm
    loop = asyncio.get_running_loop()
    model_name = Path(model_path).name
    try:
        print(f"Switching model → {model_path}")
        _emit("⚙️", "Switching model", model_name)
        # Load new model first so the old one stays alive during the load phase
        new_llm = await loop.run_in_executor(None, LLMEngine, model_path)

        old_engine = llm
        # Null vision_llm *before* freeing old engine to prevent use-after-free
        if vision_llm is old_engine:
            vision_llm = None
        llm = new_llm
        active_template  = detect_template(model_path)
        _main_model_path = model_path
        # Restore vision_llm if the new model supports it
        if new_llm.is_multimodal:
            vision_llm = new_llm
        # Free old engine now that the new one is live
        if old_engine is not None:
            await loop.run_in_executor(None, old_engine.free)
        # Persist the choice so the next restart loads this model automatically
        _save_model_pref(model_path)
        _emit("✅", "Model ready", model_name)
        print(f"Model switched. Template: {active_template}")
    except Exception as e:
        _emit("❌", "Model switch failed", str(e)[:60])
        print(f"Model switch failed: {e}")
    finally:
        switching_model = False


@app.get("/api/health")
async def health():
    return {"status": "ok", "model_loaded": llm is not None, "switching": switching_model}


@app.get("/api/activity")
async def activity_stream():
    """SSE stream of human-friendly server activity events."""
    q = _activity_subscribe()

    async def _generate():
        # Initial ping so the browser knows the connection is live
        yield f"data: {json.dumps({'icon': '🟢', 'label': 'Activity monitor connected', 'detail': ''})}\n\n"
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25.0)
                    payload = json.dumps({
                        "icon":   event.icon,
                        "label":  event.label,
                        "detail": event.detail,
                    })
                    yield f"data: {payload}\n\n"
                except TimeoutError:
                    # Keep-alive comment — prevents proxies from closing the connection
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _activity_unsubscribe(q)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Background helpers ─────────────────────────────────────────────────────────

async def _seed_knowledge_base(force: bool = False):
    """
    Crawl each seed source and index it — skipped if already indexed (unless force=True).
    Runs as a background task so startup is never delayed.
    Sources are crawled concurrently in small batches to avoid hammering servers.
    """
    to_crawl = []
    for src in _SEED_SOURCES:
        if not force and rag.has_source(src["url"]):
            continue
        to_crawl.append(src)

    if not to_crawl:
        print("[Seed] All sources already indexed.")
        return

    print(f"[Seed] Indexing {len(to_crawl)} source(s) in the background…")
    _emit("🌱", "Seeding knowledge base", f"{len(to_crawl)} sources")

    # Crawl in batches of 4 — polite to servers, still parallel
    batch_size = 4
    for i in range(0, len(to_crawl), batch_size):
        batch = to_crawl[i : i + batch_size]
        tasks = []
        for src in batch:
            source_id = src["url"]
            rag.create_source(source_id=source_id, name=src["name"], source_type="url")
            tasks.append(_crawl_and_index(source_id, source_id, max_pages=src["max_pages"]))
        await asyncio.gather(*tasks, return_exceptions=True)

    _emit("✅", "Knowledge base seeding complete")
    print("[Seed] Background seeding complete.")


async def _index_url_if_new(url: str):
    """
    Fetch a URL and add it to the knowledge base — only if not already indexed.
    Runs as a fire-and-forget background task after a web search so future
    queries on the same topic can be answered from the local knowledge base.
    """
    if rag.has_source(url):
        return
    try:
        _emit("🌐", "Opening URL", url[:60])
        page = await scraper.fetch_single(url)
        if not page or not page.get("text"):
            return
        title = page.get("title") or url
        _emit("📄", "Parsing page", title[:60])
        await rag.add_text_async(page["text"], name=title, source_id=url)
        _emit("📚", "Indexed", title[:60])
        print(f"[RAG] Background indexed: {title} ({url})")
    except Exception as e:
        print(f"[RAG] Background index failed for {url}: {e}")


async def _crawl_and_index(url: str, source_id: str, max_pages: int = 20):
    try:
        _emit("🌐", "Opening site", url[:60])
        rag.update_source_status(source_id, "crawling")
        _emit("🕷️", "Crawling", url[:60])
        pages = await scraper.crawl(url, max_pages=max_pages)
        _emit("📄", "Parsing pages", f"{len(pages)} page(s) from {url[:40]}")
        rag.update_source_status(source_id, "indexing")
        for page in pages:
            await rag.add_text_async(page["text"], name=page["url"], source_id=source_id)
        rag.update_source_status(source_id, "ready")
        _emit("✅", "Indexed", f"{len(pages)} page(s) · {url[:40]}")
        print(f"[RAG] Indexed {len(pages)} page(s) from {url}")
    except Exception as e:
        rag.update_source_status(source_id, f"error: {e}")


async def _retrain_sources(source_ids: list[str] | None):
    sources = rag.list_sources()
    targets = [s for s in sources if s["type"] == "url"]
    if source_ids:
        targets = [s for s in targets if s["id"] in source_ids]
    for source in targets:
        await _crawl_and_index(source["id"], source["id"])


# Training / fine-tuning endpoints now live in training_router.py
# (included above via app.include_router(training_router)).
