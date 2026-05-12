#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Local LLM Chat App — Start Script
# Launches FastAPI backend and opens the frontend in your browser
# ─────────────────────────────────────────────────────────────────────────────

BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
CYAN="\033[0;36m"
RESET="\033[0m"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$SCRIPT_DIR/backend"
FRONTEND="$SCRIPT_DIR/frontend"
PORT=8000
UI_PORT=3000
PID_FILE="/tmp/finance-intel.pids"

BACKEND_ONLY=false
for arg in "$@"; do
  [[ "$arg" == "--backend-only" || "$arg" == "-b" ]] && BACKEND_ONLY=true
done

# ── Check venv ───────────────────────────────────────────────────────────────
if [ ! -d "$BACKEND/venv" ]; then
  echo -e "${RED}Virtual environment not found. Run ./setup.sh first.${RESET}"
  exit 1
fi

# ── Check .env exists and MODEL_PATH is set ──────────────────────────────────
if [ ! -f "$BACKEND/.env" ]; then
  echo -e "${RED}No .env file found. Run ./setup.sh first.${RESET}"
  exit 1
fi

MODEL_PATH=$(grep "^MODEL_PATH" "$BACKEND/.env" | cut -d= -f2- | tr -d ' ')
if [ -z "$MODEL_PATH" ] || [[ "$MODEL_PATH" == *"your-model"* ]] || [[ "$MODEL_PATH" == *"/" ]]; then
  echo -e "${YELLOW}⚠️  MODEL_PATH in backend/.env doesn't point to a model file.${RESET}"
  echo -e "   Edit ${BOLD}backend/.env${RESET} and set MODEL_PATH to your .gguf file."
  echo -e "   Example: MODEL_PATH=../models/mistral-7b.gguf"
  echo ""
  read -p "Start anyway (model features will be disabled)? [y/N] " confirm
  [[ "$confirm" != "y" && "$confirm" != "Y" ]] && exit 0
fi

# ── Kill any previously tracked processes ────────────────────────────────────
if [ -f "$PID_FILE" ]; then
  echo -e "${YELLOW}Stopping previously started processes…${RESET}"
  while read -r pid; do
    kill "$pid" 2>/dev/null || true
  done < "$PID_FILE"
  sleep 1
  while read -r pid; do
    kill -9 "$pid" 2>/dev/null || true
  done < "$PID_FILE"
  rm -f "$PID_FILE"
fi

# ── Kill any existing servers on same ports ───────────────────────────────────
if lsof -ti:$PORT &>/dev/null; then
  echo -e "${YELLOW}Stopping existing process on port $PORT…${RESET}"
  lsof -ti:$PORT | xargs kill -9 2>/dev/null || true
  sleep 1
fi
if [ "$BACKEND_ONLY" = false ] && lsof -ti:$UI_PORT &>/dev/null; then
  echo -e "${YELLOW}Stopping existing process on port $UI_PORT…${RESET}"
  lsof -ti:$UI_PORT | xargs kill -9 2>/dev/null || true
  sleep 1
fi

echo ""
echo -e "${BOLD}${CYAN}Starting Local LLM Chat…${RESET}"
echo -e "  Backend:  ${BOLD}http://localhost:$PORT${RESET}"
echo -e "  Docs:     ${BOLD}http://localhost:$PORT/docs${RESET}"
if [ "$BACKEND_ONLY" = false ]; then
  echo -e "  Frontend: ${BOLD}http://localhost:$UI_PORT${RESET}"
fi
echo ""

UI_PID=""

# ── Start frontend dev server ─────────────────────────────────────────────────
if [ "$BACKEND_ONLY" = false ]; then
  cd "$FRONTEND"
  npm run dev > /tmp/finance-intel-ui.log 2>&1 &
  UI_PID=$!

  echo -n "Waiting for UI server"
  for i in $(seq 1 30); do
    if curl -s "http://localhost:$UI_PORT" &>/dev/null; then
      echo ""
      echo -e "${GREEN}✓ UI server is up${RESET}"
      break
    fi
    echo -n "."
    sleep 1
  done
  echo ""
fi

# ── Start backend ─────────────────────────────────────────────────────────────
cd "$BACKEND"
source venv/bin/activate

python3 -m uvicorn main:app --host 0.0.0.0 --port $PORT --log-level info &
SERVER_PID=$!

# ── Save PIDs so a future run (or manual kill) can clean them up ──────────────
echo "$SERVER_PID" > "$PID_FILE"
[ -n "$UI_PID" ] && echo "$UI_PID" >> "$PID_FILE"

# ── Wait for server to be ready ──────────────────────────────────────────────
echo -n "Waiting for server"
for i in $(seq 1 30); do
  if curl -s "http://localhost:$PORT/api/health" &>/dev/null; then
    echo ""
    echo -e "${GREEN}✓ Server is up${RESET}"
    break
  fi
  echo -n "."
  sleep 1
done
echo ""

# ── Open frontend in browser ─────────────────────────────────────────────────
if [ "$BACKEND_ONLY" = false ]; then
  echo -e "${BOLD}Opening frontend…${RESET}"
  open "http://localhost:$UI_PORT"
fi

echo ""
echo -e "${GREEN}App is running!${RESET}"
echo -e "Press ${BOLD}Ctrl+C${RESET} to stop."
echo ""

# ── Shut down cleanly on Ctrl+C or terminal close ────────────────────────────
cleanup() {
  echo ""
  echo -e "${YELLOW}Shutting down…${RESET}"
  kill "$SERVER_PID" $UI_PID 2>/dev/null || true
  sleep 1
  kill -9 "$SERVER_PID" $UI_PID 2>/dev/null || true
  rm -f "$PID_FILE"
  exit 0
}
trap cleanup INT TERM HUP

wait $SERVER_PID
