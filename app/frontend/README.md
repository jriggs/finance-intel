# Finance Frontend

Static single-page app for the Finance Intelligence backend. Built with TypeScript + esbuild. No framework.

## Setup

```bash
npm install
```

## Development

```bash
npm run dev
```

Starts esbuild in watch mode and serves the app at **http://localhost:3000**. The browser reloads automatically when any `src/` file changes.

## Production build

```bash
npm run build
```

Outputs `dist/main.js` (~60kb). The FastAPI backend serves `index.html` directly — no extra deploy step needed.

## Other commands

```bash
npm run typecheck   # tsc --noEmit (type errors only, no output)
npm run lint        # ESLint + typescript-eslint
```

## Source layout

```
src/
├── types.ts        shared interfaces + LightweightCharts global declaration
├── api.ts          apiFetch wrapper
├── utils.ts        $(), fmt, fmtDollar, fmtPct, fmtBig, colorPct
├── state.ts        global mutable state with typed setters
├── router.ts       hash-based routing (parseHash, setHash, navigateToStock)
├── dashboard.ts    dashboard view
├── watchlist.ts    sidebar watchlist CRUD
├── stock.ts        stock detail, signal/fundamentals/news renderers
├── chart.ts        LightweightCharts candlestick + SMA overlays
├── signals.ts      watchlist signals, screeners, recommendations
├── volume.ts       NYSE volume analysis table + sorting
├── picks.ts        picks tracker
├── broker.ts       Alpaca portfolio, orders
├── feed.ts         news & social feed tabs
├── chat.ts         AI chat stream, insight generation
├── settings.ts     settings panel, volume/screener/cache config
├── activities.ts   scheduled jobs + log viewer
└── main.ts         router(), init(), event wiring, window bindings
```
