#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Local LLM Chat App — M1/M2/M3 Mac Setup Script
# Builds llama-cpp-python with Apple Metal (GPU) acceleration
# ─────────────────────────────────────────────────────────────────────────────

set -e
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
CYAN="\033[0;36m"
RESET="\033[0m"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$SCRIPT_DIR/backend"
MODELS="$SCRIPT_DIR/models"

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║       Local LLM Chat — M1 Mac Setup      ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════╝${RESET}"
echo ""

# ── 1. Check Homebrew ────────────────────────────────────────────────────────
echo -e "${BOLD}[1/7] Checking Homebrew…${RESET}"
if ! command -v brew &>/dev/null; then
  echo -e "${YELLOW}Homebrew not found. Installing…${RESET}"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
else
  echo -e "${GREEN}✓ Homebrew found${RESET}"
fi

# ── 2. Check Python ──────────────────────────────────────────────────────────
echo -e "\n${BOLD}[2/7] Checking Python…${RESET}"
if ! command -v python3 &>/dev/null; then
  echo "Installing Python via Homebrew…"
  brew install python@3.11
fi
PYTHON=$(command -v python3.11 || command -v python3)
PY_VER=$($PYTHON --version 2>&1)
echo -e "${GREEN}✓ $PY_VER${RESET}"

# ── 3. Create virtual environment ────────────────────────────────────────────
echo -e "\n${BOLD}[3/7] Setting up Python virtual environment…${RESET}"
cd "$BACKEND"
if [ ! -d "venv" ]; then
  $PYTHON -m venv venv
  echo -e "${GREEN}✓ Virtual environment created at backend/venv${RESET}"
else
  echo -e "${GREEN}✓ Virtual environment already exists${RESET}"
fi

source venv/bin/activate

# Upgrade pip quietly
pip install --upgrade pip -q

# ── 4. Install mlx-lm (fine-tuning on Apple Silicon) ────────────────────────
echo -e "\n${BOLD}[4/7] Installing mlx-lm (LoRA fine-tuning)…${RESET}"
if python -c "import mlx_lm" &>/dev/null; then
  echo -e "${GREEN}✓ mlx-lm already installed${RESET}"
else
  pip install mlx-lm -q
  echo -e "${GREEN}✓ mlx-lm installed${RESET}"
fi

# ── 5. Install llama-cpp-python with Metal ───────────────────────────────────
echo -e "\n${BOLD}[5/7] Installing llama-cpp-python with Apple Metal…${RESET}"
echo -e "${CYAN}This compiles from source — takes 2–5 minutes on first run${RESET}"

# Check if already installed with Metal
if python -c "from llama_cpp import Llama; print('ok')" &>/dev/null; then
  echo -e "${GREEN}✓ llama-cpp-python already installed${RESET}"
  echo -e "${YELLOW}  To reinstall with Metal: CMAKE_ARGS=\"-DGGML_METAL=on\" pip install llama-cpp-python --force-reinstall${RESET}"
else
  CMAKE_ARGS="-DGGML_METAL=on" pip install llama-cpp-python
  echo -e "${GREEN}✓ llama-cpp-python installed with Metal support${RESET}"
fi

# ── 5. Install remaining dependencies ───────────────────────────────────────
echo -e "\n${BOLD}[6/7] Installing remaining dependencies…${RESET}"
# Install everything except llama-cpp-python (already handled)
grep -v "llama-cpp-python" requirements.txt > /tmp/req_no_llama.txt
pip install -r /tmp/req_no_llama.txt -q
rm /tmp/req_no_llama.txt
echo -e "${GREEN}✓ All dependencies installed${RESET}"

echo -e "${GREEN}✓ Finance dependencies (alpaca-py, APScheduler, playwright) already in venv${RESET}"

# ── 6. Configure .env ────────────────────────────────────────────────────────
echo -e "\n${BOLD}[7/7] Configuring environment…${RESET}"
if [ ! -f "$BACKEND/.env" ]; then
  cp "$BACKEND/.env.example" "$BACKEND/.env"

  # M1 Pro 16GB optimized defaults
  sed -i '' 's|MODEL_PATH=.*|MODEL_PATH=../models/|' "$BACKEND/.env"
  sed -i '' 's|N_CTX=.*|N_CTX=4096|' "$BACKEND/.env"
  sed -i '' 's|N_GPU_LAYERS=.*|N_GPU_LAYERS=-1|' "$BACKEND/.env"  # full Metal offload
  sed -i '' 's|N_THREADS=.*|N_THREADS=8|' "$BACKEND/.env"          # M1 Pro has 8 perf cores

  echo -e "${GREEN}✓ .env created with M1 Pro optimized settings${RESET}"
else
  echo -e "${GREEN}✓ .env already exists (skipped)${RESET}"
fi

# ── Create models dir ────────────────────────────────────────────────────────
mkdir -p "$MODELS"

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  ✅  Setup complete!${RESET}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════${RESET}"
echo ""
echo -e "${BOLD}Next step: Download a model${RESET}"
echo ""
echo -e "  ${CYAN}Recommended for M1 Pro 16GB:${RESET}"
echo ""
echo -e "  ${BOLD}Option A — Phi-3 Mini (2.2GB, fastest):${RESET}"
echo -e "  curl -L -o models/phi-3-mini.gguf \\"
echo -e "    'https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf'"
echo ""
echo -e "  ${BOLD}Option B — Mistral 7B Instruct (4.1GB, balanced):${RESET}"
echo -e "  curl -L -o models/mistral-7b.gguf \\"
echo -e "    'https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.2-GGUF/resolve/main/mistral-7b-instruct-v0.2.Q4_K_M.gguf'"
echo ""
echo -e "  ${BOLD}Option C — Llama 3 8B Instruct (4.7GB, best quality):${RESET}"
echo -e "  See: https://huggingface.co/bartowski/Meta-Llama-3-8B-Instruct-GGUF"
echo ""
echo -e "  Then set ${BOLD}MODEL_PATH${RESET} in ${BOLD}backend/.env${RESET} to point to the file."
echo ""
echo -e "  When ready, run:  ${BOLD}./start.sh${RESET}"
echo ""
