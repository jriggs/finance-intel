# Backend

FastAPI server for the Finance Intelligence Platform. Handles LLM inference, finance data, RAG, trading, and web scraping — all locally on Apple Silicon.

## Setup

Run once from `app/`:

```bash
./setup.sh
```

This creates `backend/venv/`, compiles `llama-cpp-python` with Metal GPU support, and installs all dependencies.

## Configuration

```bash
cp .env.example .env
```

Minimum required:

```ini
MODEL_PATH=../models/your-model.gguf
```

Optional API keys (finance features work without them but are enhanced):

```ini
ALPACA_API_KEY=...      # paper/live trading — alpaca.markets
ALPACA_SECRET_KEY=...
FRED_API_KEY=...        # macro data — fred.stlouisfed.org
OPENFIGI_API_KEY=...    # ticker normalization (raises rate limits)
```

Full variable reference in the root `README.md`.

## Running

```bash
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000
```

Or use `./start.sh` from `app/` which handles activation automatically.

## Testing

```bash
./run_tests.sh           # from app/
# or directly:
source venv/bin/activate
pytest tests/ -v
pytest tests/ -k signals  # run matching subset
```

## Module layout

```
backend/
├── main.py             FastAPI app entry point — chat, RAG, model hot-swap, vision, SSE streaming
├── finance_router.py   All /api/finance/* endpoints, mounted into main app
│
├── market.py           yfinance: live quotes, OHLCV history, RSI/MACD/Bollinger/SMA, rate-limit backoff
├── signals.py          Signal scoring — value (P/E, P/B, EV/EBITDA, FCF), technicals, analyst consensus → A–D
├── fundamentals.py     SEC EDGAR XBRL, FRED macro, Macrotrends historical, OpenFIGI ticker normalization
├── db.py               SQLite persistence layer — all app state in a single finance.db
├── broker.py           Alpaca trading: orders, positions, P&L (paper + live)
├── crawler.py          Playwright news crawlers — Reddit, MarketWatch, Finviz, Polymarket, Seeking Alpha
├── scheduler.py        APScheduler background jobs: crawling, signal scoring, EDGAR ingestion
├── ingestion.py        RAG pipeline — documents/filings → chunk → embed → ChromaDB
│
├── llm.py              llama-cpp-python wrapper: model load, Metal GPU config, streaming, vision auto-swap
├── rag.py              ChromaDB wrapper: chunking, sentence-transformer embeddings, similarity retrieval
├── scraper.py          Async HTTP scraper: trafilatura + BeautifulSoup, connection pool
├── search.py           Web search: DuckDuckGo, Google News RSS, URL extraction, content injection
├── trainer.py          MLX LoRA fine-tuning: dataset upload, training loop, live loss stream, GGUF export
│
├── prompts.py          Prompt template selection by model family + system prompt construction
├── activity.py         SSE event emitter — broadcasts real-time job progress to the UI
├── http_retry.py       Exponential backoff decorator for external HTTP calls
├── refresh_symbols.py  Bulk ticker lookup via OpenFIGI/yfinance, populates symbols autocomplete table
│
├── tests/              Unit + integration tests (pytest)
├── .env                Local secrets (gitignored)
├── .env.example        Config template
└── requirements.txt    Python dependencies
```

## Key patterns

**LLM hot-swap** — `main.py` holds a single `LLMEngine` instance behind `_swap_lock`. Swap requests queue; the vision model loads/unloads on demand without restart. Last-chosen model persists to `app_settings` via `db.set_setting()`.

**SSE streaming** — Chat and analyst responses stream via Server-Sent Events. `activity.py` broadcasts background job progress on a separate `/api/activity` stream so the UI can show scheduler status without polling.

**Rate limiting** — yfinance enforces ~100–150 concurrent requests. `market.py` uses exponential backoff (0.5 → 1.0 → 2.0s). `signals.py` throttles 0.2–0.3s between stocks. Crawlers run sequentially to avoid thundering herd.

**Single DB** — All app state lives in `../data/finance.db` (SQLite). `db.py` owns the schema and all read/write helpers. ChromaDB is separate at `../data/chroma/` — it's a vector index managed by the ChromaDB library.

**Background jobs** — `scheduler.py` runs three APScheduler jobs: periodic news crawl, signal recalculation for the watchlist, and EDGAR filing ingestion. Jobs coordinate through the shared `db.py` layer.
