# Finance Intelligence Platform

A full-stack, locally-run AI-powered finance dashboard. Runs any GGUF LLM model locally on Apple Silicon with Metal GPU acceleration—no cloud API required.

**Two integrated interfaces:**
- **Chat Interface** (`http://localhost:8000`) — General-purpose AI chat with web search, RAG, vision analysis, and model fine-tuning
- **Finance Dashboard** (`http://localhost:8000/finance`) — Market intelligence: real-time quotes, technical signals, paper trading, SEC filings analysis, macro indicators, and AI-powered insights

All data processing and LLM inference runs locally. Optional integrations available for live trading (Alpaca), macro data (FRED), and ticker normalization (OpenFIGI).

---

## Tech Stack

| Component | Technology |
|---|---|
| **Backend** | FastAPI + Server-Sent Events (SSE) streaming |
| **LLM Inference** | `llama-cpp-python` with GGUF models and Apple Metal acceleration |
| **Embeddings** | `nomic-embed-text-v1.5` (sentence-transformers) |
| **Vector Store** | ChromaDB (chat RAG + finance filings) |
| **Database** | SQLite — single `finance.db` for all app state |
| **Market Data** | yfinance (prices, OHLCV, technicals, news) |
| **SEC Filings** | EDGAR API + XBRL company facts |
| **Macro Data** | FRED (Federal Reserve Economic Data) |
| **Ticker Normalization** | OpenFIGI |
| **Web Crawling** | Playwright (Reddit, MarketWatch, Finviz, Polymarket, Seeking Alpha) |
| **Task Scheduling** | APScheduler (crawls, signal scoring, RAG ingestion) |
| **Broker Integration** | Alpaca (paper + live US equities trading) |
| **Web Search** | DuckDuckGo, Google News RSS |
| **Web Scraping** | trafilatura + BeautifulSoup |
| **Fine-Tuning** | `mlx-lm` (LoRA on Apple Silicon) |
| **Finance Frontend** | TypeScript + esbuild (no framework, ~60kb bundle) |

---

## Quick Start

### 1. Download an LLM Model

Place a GGUF quantized model in `app/models/`:

```bash
# Option A: Qwen 2.5 14B (~32GB RAM, excellent reasoning)
curl -L -o app/models/qwen-14b.gguf \
  'https://huggingface.co/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct.Q4_K_M.gguf'

# Option B: Llama 3.1 8B (~16GB RAM, balanced)
curl -L -o app/models/llama-8b.gguf \
  'https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf'

# Option C: Mistral 7B (~14GB RAM, fast)
curl -L -o app/models/mistral-7b.gguf \
  'https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.2-GGUF/resolve/main/mistral-7b-instruct-v0.2.Q4_K_M.gguf'
```

### 2. One-time Setup

```bash
cd app
chmod +x setup.sh start.sh
./setup.sh
```

This:
- Creates a Python virtual environment in `backend/venv/`
- Compiles `llama-cpp-python` with Metal GPU acceleration for Apple Silicon
- Installs all dependencies (FastAPI, finance libraries, web scrapers, etc.)

### 3. Configure

Copy the template and fill in your values:

```bash
cp app/backend/.env.example app/backend/.env
```

**Minimum required:**

```ini
MODEL_PATH=../models/your-model.gguf
```

**Optional API keys** (finance features work without these, but are enhanced by them):

```ini
ALPACA_API_KEY=...       # Paper/live trading — free at alpaca.markets
ALPACA_SECRET_KEY=...
FRED_API_KEY=...         # Macro data — free at fred.stlouisfed.org
OPENFIGI_API_KEY=...     # Ticker normalization (optional, raises rate limits)
```

### 4. Run

```bash
cd app
./start.sh
```

Then open:
- **Chat** → `http://localhost:8000`
- **Finance** → `http://localhost:8000/finance`

Or start the API manually:

```bash
cd app/backend
source ../venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

---

## Features

### Chat Interface

- **Multi-turn conversations** with real-time streaming and stop-generation controls
- **Web access** — URL fetching, DuckDuckGo search, Google News RSS feed
- **Vision** — Image upload with LLaVA/Llama-Vision/Qwen-VL support; auto model-swap for vision tasks
- **RAG (Retrieval-Augmented Generation)** — Crawl URLs, paste text, ChromaDB vector search, toggle per-session
- **Hot-swap models** from the UI — no restart needed
- **Fine-tuning** — Upload JSONL/CSV/TXT, train LoRA adapters with live loss monitoring, export to GGUF

### Finance Dashboard

#### Market Intelligence
- **Signal Scoring** — Value metrics (P/E, P/B, EV/EBITDA, FCF), technicals (RSI, SMA, MACD, Bollinger bands), analyst consensus → graded A–D
- **Candlestick Charts** — OHLCV + volume + SMA overlays (20/50/200) via TradingView Lightweight Charts
- **Watchlist** — Live quotes, per-symbol signal grades, custom tags
- **Screeners** — Filter stocks by technical/fundamental criteria

#### Portfolio & Trading
- **Paper Trading** — Alpaca integration for market/limit orders, position tracking, P&L calculations
- **Picks Tracker** — Log model recommendations, track vs entry price, log outcome, view win-rate stats

#### Research
- **SEC EDGAR** — 10-K, 10-Q, 8-K filings indexed in ChromaDB; LLM answers questions from actual filing text
- **XBRL Financials** — 5-year revenue, net income, EPS, cash flow, assets, debt extracted from EDGAR
- **FRED Macro** — GDP, CPI, Fed Funds rate, unemployment, yield curve, VIX, M2, retail sales injected into LLM context
- **News & Social** — Crawled from Reddit finance subs, MarketWatch, Finviz, Polymarket, Seeking Alpha (scheduled, sequential)

#### AI Analysis
- **AI Analyst** — LLM with auto-injected market context: live prices, news, FRED macro data, RAG-retrieved filings → streaming Q&A

---

## Project Structure

```
app/
├── backend/
│   ├── main.py             # FastAPI app, chat + RAG + model routes
│   ├── finance_router.py   # All /api/finance/* endpoints
│   ├── market.py           # yfinance: prices, OHLCV, technicals
│   ├── signals.py          # Signal scoring (value + technical + analyst)
│   ├── fundamentals.py     # SEC EDGAR, FRED, OpenFIGI, Macrotrends
│   ├── ingestion.py        # RAG ingestion (filings → ChromaDB)
│   ├── broker.py           # Alpaca trading
│   ├── db.py               # SQLite persistence layer (finance.db)
│   ├── crawler.py          # Playwright web crawlers (throttled, sequential)
│   ├── scheduler.py        # APScheduler jobs
│   ├── llm.py              # llama-cpp-python wrapper with Metal
│   ├── rag.py              # ChromaDB + embeddings
│   ├── scraper.py          # Async web scraper (trafilatura + BS4)
│   ├── search.py           # Web search routing (DuckDuckGo, Google News RSS)
│   ├── trainer.py          # MLX LoRA fine-tuning
│   ├── prompts.py          # Prompt templates + model family detection
│   ├── activity.py         # SSE event emitter for real-time UI updates
│   ├── http_retry.py       # Exponential backoff for HTTP requests
│   ├── refresh_symbols.py  # Ticker lookup + symbol cache refresh
│   ├── requirements.txt    # Python dependencies
│   ├── .env                # Your local config (gitignored)
│   ├── .env.example        # Config template — copy to .env to get started
│   ├── tests/              # Unit + integration tests
│   └── venv/               # Virtual environment (gitignored)
├── frontend/
│   ├── src/
│   │   ├── types.ts        # Shared interfaces
│   │   ├── api.ts          # API client
│   │   ├── utils.ts        # Formatters, helpers
│   │   ├── state.ts        # Global mutable state
│   │   ├── router.ts       # Hash-based routing
│   │   ├── dashboard.ts    # Dashboard view
│   │   ├── watchlist.ts    # Watchlist CRUD
│   │   ├── stock.ts        # Stock detail view
│   │   ├── chart.ts        # Candlestick chart view
│   │   ├── signals.ts      # Signal + screener views
│   │   ├── picks.ts        # Picks tracker
│   │   ├── broker.ts       # Alpaca portfolio
│   │   ├── feed.ts         # News & social feed
│   │   ├── chat.ts         # AI chat
│   │   ├── volume.ts       # Volume analysis view
│   │   ├── activities.ts   # Activity log viewer
│   │   ├── settings.ts     # Settings + admin panel
│   │   └── main.ts         # Entry point
│   ├── index.html          # Single-page app shell
│   ├── eslint.config.js
│   ├── tsconfig.json
│   └── package.json
├── train/
│   ├── train.py            # MLX LoRA training script
│   ├── prepare_data.py     # Data preparation
│   ├── export.py           # Export adapters → GGUF
│   └── adapters/           # Trained LoRA adapters (gitignored)
├── models/                 # Your GGUF files — add them here (gitignored)
├── data/                   # Auto-created at runtime (gitignored)
│   ├── finance.db          # Single SQLite DB — all app state
│   └── chroma/             # ChromaDB vector index
├── setup.sh                # One-time setup
└── start.sh                # Run the app
```

---

## Backend Modules

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app entry point. Chat, RAG, model hot-swap, vision, SSE streaming, lifespan setup |
| `finance_router.py` | All `/api/finance/*` endpoints — mounts into main app, consumes shared LLM/RAG singletons |
| `market.py` | yfinance wrapper: live quotes, OHLCV price history, technicals (RSI, MACD, Bollinger bands, SMA), rate-limit backoff |
| `signals.py` | Signal scoring engine: value metrics (P/E, P/B, EV/EBITDA, FCF yield), technicals, analyst consensus → A–D grades |
| `fundamentals.py` | External data: SEC EDGAR XBRL financials, FRED macro indicators, Macrotrends historical data, OpenFIGI ticker normalization |
| `db.py` | SQLite persistence layer for all app state: watchlist, picks, crawl cache, symbols, sentiment, settings, RAG sources |
| `broker.py` | Alpaca trading integration: place market/limit orders, fetch positions, P&L, account info (paper + live) |
| `crawler.py` | Playwright-based news crawlers: Reddit finance subs, MarketWatch, Finviz, Polymarket, Seeking Alpha — throttled sequential execution |
| `scheduler.py` | APScheduler background jobs: periodic crawling, signal recalculation, EDGAR filing ingestion |
| `ingestion.py` | RAG ingestion pipeline: parse documents/filings → chunk → embed → store in ChromaDB |
| `rag.py` | ChromaDB wrapper: document chunking, sentence-transformer embeddings, similarity retrieval |
| `llm.py` | llama-cpp-python wrapper: model loading, Metal GPU config, streaming inference, vision model auto-swap |
| `scraper.py` | Async HTTP scraper: trafilatura + BeautifulSoup content extraction, connection pool management |
| `search.py` | Web search routing: DuckDuckGo, Google News RSS, URL extraction, scraped content injection into LLM context |
| `trainer.py` | MLX LoRA fine-tuning: dataset upload, hyperparameter config, live loss streaming, GGUF export |
| `prompts.py` | Prompt template selection by model family (Llama, Mistral, Qwen, Phi, etc.) + system prompt construction |
| `activity.py` | Server-Sent Events emitter for broadcasting real-time progress updates to the UI |
| `http_retry.py` | Exponential backoff decorator for external HTTP calls with configurable retry policies |
| `refresh_symbols.py` | Bulk ticker lookup via OpenFIGI/yfinance; populates the symbols autocomplete table in SQLite |

---

## Data Storage

All application state is stored in a **single SQLite database** at `app/data/finance.db`. Nothing is scattered across multiple files or directories.

| Table | Contents |
|---|---|
| `watchlist` | Tracked symbols |
| `picks` | Model-recommended trades + outcomes |
| `crawl_cache` | Scraped web content, scored results |
| `symbols` | Ticker + company name for autocomplete |
| `sentiment` | Per-symbol sentiment cache |
| `rag_sources` | RAG document sources (replaces `sources.json`) |
| `app_settings` | Key-value settings, e.g. last-selected model |
| `screener_results` | Screener output cache |
| `recommendations` | AI-generated stock recommendations |
| `volume_analysis_*` | Volume scoring tasks + results |
| `extended_insights` | Per-symbol AI-generated insights |

ChromaDB (`data/chroma/`) is separate — it's a vector index managed by the ChromaDB library and can't be merged into SQLite without replacing the library.

---

## Environment Variables

### Chat / LLM Core

| Variable | Default | Description |
|---|---|---|
| `MODEL_PATH` | *(required)* | Path to GGUF model file |
| `VISION_MODEL_PATH` | *(auto-detect)* | Vision model path (if different) |
| `N_CTX` | `4096` | Context window (tokens) |
| `N_GPU_LAYERS` | `-1` | GPU layers (`-1` = all on Metal) |
| `N_THREADS` | `8` | CPU threads for inference |
| `EMBED_MODEL` | `nomic-ai/nomic-embed-text-v1.5` | RAG embedding model |
| `CHUNK_SIZE` | `512` | Tokens per RAG chunk |
| `DATA_DIR` | `../data` | Path to `finance.db` + ChromaDB |
| `HF_TOKEN` | *(optional)* | HuggingFace token (for fine-tuning) |

### Finance

| Variable | Default | Description |
|---|---|---|
| `ALPACA_API_KEY` | *(optional)* | Alpaca API key (trading) |
| `ALPACA_SECRET_KEY` | *(optional)* | Alpaca secret key |
| `ALPACA_PAPER` | `true` | Paper trading mode |
| `FRED_API_KEY` | *(optional)* | FRED API key (macro data) |
| `OPENFIGI_API_KEY` | *(optional)* | OpenFIGI API key |
| `CRAWL_INTERVAL_MIN` | `60` | Crawler interval (minutes) |

---

## Rate Limiting & Resilience

**yfinance** has strict rate limits (~100–150 concurrent requests). The app handles this transparently:

- **Exponential backoff** on market data fetches (0.5s → 1.0s → 2.0s delays)
- **Request throttling** in signal scoring (0.2–0.3s between stock scores)
- **Sequential crawlers** (Reddit, MarketWatch, etc.) to avoid thundering herd
- **Graceful degradation** — missing data doesn't break the UI

With 2,000 stocks, full scoring takes ~33 hours but runs in background. Charts, quotes, and picks work independently.

---

## Fine-tuning

Upload a dataset and train a LoRA adapter on your local GPU:

1. **Train tab** → Upload JSONL/CSV/TXT
   - JSONL format: `{"prompt":"Q: ...","completion":"A: ..."}`
   - CSV: columns `prompt,completion`
   - TXT: one example per line

2. Configure hyperparameters → **Start Training**
   - Watch live loss curve
   - Adapters save to `train/adapters/<dataset>/`

3. Export to GGUF:

```bash
cd app/train
python export.py \
  --model mistralai/Mistral-7B-Instruct-v0.2 \
  --adapters ./adapters/my-dataset \
  --output ../models/my-finetuned.gguf \
  --quantize q4_k_m
```

4. Update `MODEL_PATH` in `backend/.env` and restart.

---

## Development

### Run Backend Tests

```bash
cd app
./run_tests.sh

# Or directly:
cd app/backend
source venv/bin/activate
python -m pytest tests/ -v
```

### Finance Frontend Dev Server

```bash
cd app/frontend
npm install
npm run dev       # hot reload at http://localhost:3000
npm run typecheck # type errors only
npm run lint      # ESLint
```

### Production Frontend Build

```bash
cd app/frontend
npm run build
```

Outputs `dist/main.js` (~60kb). FastAPI serves `index.html` directly — no deploy step needed.

---

## Troubleshooting

### Model Loading Issues

```bash
# Check Metal compilation
python -c "import llama_cpp; print(llama_cpp.__version__)"

# Test model load
cd app/backend && source venv/bin/activate
python -c "from llm import LLMEngine; print('OK')"
```

### Rate Limiting on Market Data

Check logs for `Rate limited, retry X/Y`. This is normal under load. Signal scoring runs in background; watchlist updates work immediately.

### Finance Data Missing

- Verify `ALPACA_API_KEY` and `FRED_API_KEY` in `backend/.env`
- Check scheduler logs for crawler errors
- XBRL data takes 10–20 seconds per ticker (runs async)

### Fine-tuning Crashes

Ensure:
- `N_GPU_LAYERS=-1` (uses Metal, not CPU-only)
- Model is supported by `mlx-lm` (Llama, Mistral, Qwen, etc.)
- Dataset is valid JSONL/CSV/TXT

---

## API Endpoints (Selected)

### Chat
- `POST /api/chat` — Stream chat with RAG toggle, model selection
- `POST /api/sources/url` — Crawl URL into RAG
- `GET /api/models` — List available GGUF models
- `POST /api/train/start` — Start LoRA fine-tuning
- `GET /api/train/status` — Training progress

### Finance
- `GET /api/finance/quote/{symbol}` — Current price + technicals
- `GET /api/finance/chart/{symbol}` — OHLCV + SMA (125 days)
- `GET /api/finance/signals/{symbol}` — Signal scores + grades
- `GET /api/finance/watchlist` — Saved watchlist
- `POST /api/finance/watchlist` — Add/update stock
- `GET /api/finance/picks` — Picks tracker
- `POST /api/finance/order` — Place order (Alpaca)
- `GET /api/finance/analyst` — AI analyst with context injection

---

## Deployment Notes

This app is **designed for local use only**:
- No authentication (assumes trusted local network)
- No rate limiting on `/api/` endpoints
- API keys stored in plaintext in `.env`

For cloud deployment:
1. Add authentication (OAuth, API keys)
2. Move secrets to environment variables or secrets manager
3. Add rate limiting middleware
4. Proxy LLM inference through a secure backend
5. Consider containerization (Docker)

---

## License & Attribution

This project integrates multiple open-source libraries. See individual LICENSE files in dependencies and respect their terms.

**Key projects:**
- `llama-cpp-python` (MIT)
- `sentence-transformers` (Apache 2.0)
- `FastAPI` (MIT)
- `yfinance` (Apache 2.0)
- `APScheduler` (MIT)
- `ChromaDB` (Apache 2.0)
