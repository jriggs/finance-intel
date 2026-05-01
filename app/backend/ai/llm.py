"""
LLM Engine — spawns llama-server as a subprocess and communicates via its
OpenAI-compatible HTTP API.  The model runs in a separate process so its
weights never touch Python's heap, keeping the FastAPI process lean.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx


def find_mmproj(model_path: str) -> str | None:
    """
    Look for a vision projector (mmproj) file that belongs to this specific model.
    Requires at least 2 meaningful name parts to match — no catch-all fallback.
    """
    model_path = Path(model_path).resolve()
    stem = model_path.stem.lower()

    search_dirs = [
        model_path.parent,
        model_path.parent.parent,
        model_path.parent.parent / "models",
    ]

    candidates = []
    for d in search_dirs:
        candidates += list(d.glob("*mmproj*")) + list(d.glob("*mm-proj*"))

    if not candidates:
        return None

    name_parts = [p for p in stem.replace("-", " ").replace("_", " ").split() if len(p) > 1]
    best, best_score = None, 0
    for c in candidates:
        c_lower = c.name.lower()
        score = sum(1 for part in name_parts if part in c_lower)
        if score > best_score:
            best, best_score = c, score

    return str(best) if best_score >= 2 else None


def _find_llama_server() -> str:
    env_bin = os.getenv("LLAMA_SERVER_BIN", "")
    if env_bin and Path(env_bin).exists():
        return env_bin

    candidates = [
        "llama-server",
        "/opt/homebrew/bin/llama-server",
        "/usr/local/bin/llama-server",
        str(Path.home() / "llama.cpp" / "llama-server"),
        str(Path.home() / "llama.cpp" / "build" / "bin" / "llama-server"),
    ]
    for c in candidates:
        if shutil.which(c):
            return c
    raise RuntimeError(
        "llama-server binary not found.\n"
        "Install via: brew install llama.cpp  OR  build from source.\n"
        "Or set LLAMA_SERVER_BIN=/path/to/llama-server in your .env"
    )


class LLMEngine:
    def __init__(self, model_path: str, n_gpu_layers: int | None = None):
        self.model_path = model_path
        self.is_multimodal = False
        self._process: subprocess.Popen | None = None

        n_gpu_layers = n_gpu_layers if n_gpu_layers is not None else int(os.getenv("N_GPU_LAYERS", "-1"))
        n_ctx        = int(os.getenv("N_CTX", "4096"))
        n_threads    = int(os.getenv("N_THREADS", "8"))
        self._port   = int(os.getenv("LLAMA_SERVER_PORT", "8181"))
        self._base   = f"http://127.0.0.1:{self._port}"
        self._n_ctx  = n_ctx

        mmproj_path = find_mmproj(model_path)
        model_name  = Path(model_path).name.lower()

        _NO_VISION_FAMILIES  = ("gemma",)
        _CPU_ONLY_FAMILIES   = ("gemma-4", "gemma4")

        vision_unsupported = any(k in model_name for k in _NO_VISION_FAMILIES)
        cpu_only           = any(k in model_name for k in _CPU_ONLY_FAMILIES)

        if cpu_only and n_gpu_layers != 0:
            print("  Gemma 4 ISWA: forcing CPU-only (n_gpu_layers=0)")
            n_gpu_layers = 0

        cmd = [
            _find_llama_server(),
            "--model",       model_path,
            "--ctx-size",    str(n_ctx),
            "--n-gpu-layers", str(n_gpu_layers),
            "--threads",     str(n_threads),
            "--batch-size",  "512",
            "--port",        str(self._port),
            "--host",        "127.0.0.1",
            "--log-disable",
        ]

        if mmproj_path and not vision_unsupported:
            print(f"  Vision projector found: {Path(mmproj_path).name}")
            cmd += ["--mmproj", mmproj_path]
            self.is_multimodal = True
        elif mmproj_path and vision_unsupported:
            print("  mmproj found but skipped — no vision handler for this model family yet")

        print(f"  n_ctx={n_ctx}, n_gpu_layers={n_gpu_layers}, n_threads={n_threads}, port={self._port}")
        self._process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_for_ready()

    def _wait_for_ready(self, timeout: float = 120.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._process and self._process.poll() is not None:
                raise RuntimeError(f"llama-server exited with code {self._process.returncode}")
            with contextlib.suppress(Exception):
                r = httpx.get(f"{self._base}/health", timeout=2.0)
                if r.status_code == 200:
                    print("  llama-server ready")
                    return
            time.sleep(0.5)
        raise RuntimeError("llama-server did not become ready within 120 s")

    # ── Public introspection ───────────────────────────────────────────────────

    def params(self) -> dict:
        with contextlib.suppress(Exception):
            r = httpx.get(f"{self._base}/props", timeout=5.0)
            data = r.json()
            n_ctx = data.get("default_generation_settings", {}).get("n_ctx", self._n_ctx)
            return {"n_ctx": n_ctx, "n_gpu_layers": int(os.getenv("N_GPU_LAYERS", "-1")), "multimodal": self.is_multimodal}
        return {"n_ctx": self._n_ctx, "n_gpu_layers": -1, "multimodal": self.is_multimodal}

    def n_ctx(self) -> int:
        return self.params().get("n_ctx", self._n_ctx)

    def tokenize(self, text: str) -> list[int]:
        with contextlib.suppress(Exception):
            r = httpx.post(f"{self._base}/tokenize", json={"content": text}, timeout=10.0)
            return r.json().get("tokens", [])
        return []

    def free(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=5)
            with contextlib.suppress(Exception):
                self._process.kill()
        self._process = None

    # ── Text streaming ─────────────────────────────────────────────────────────

    async def stream(
        self,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> AsyncGenerator[str, None]:
        payload = {
            "prompt":         prompt,
            "temperature":    temperature,
            "n_predict":      max_tokens,
            "top_p":          float(os.getenv("TOP_P", "0.95")),
            "top_k":          int(os.getenv("TOP_K", "40")),
            "repeat_penalty": float(os.getenv("REPEAT_PENALTY", "1.1")),
            "stop": ["</s>", "[INST]", "[/INST]", "<|im_end|>",
                     "<|eot_id|>", "<|end|>", "<end_of_turn>", "<|endoftext|>"],
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", f"{self._base}/completion", json=payload) as resp:
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw == "[DONE]":
                            break
                        chunk = json.loads(raw)
                        token = chunk.get("content", "")
                        if token:
                            yield token
                        if chunk.get("stop"):
                            break
        except Exception as e:
            yield f"\n[Error: {e}]"

    # ── Vision streaming ───────────────────────────────────────────────────────

    async def stream_vision(
        self,
        messages: list,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> AsyncGenerator[str, None]:
        if not self.is_multimodal:
            yield "[This model does not support images. Load a vision model first.]"
            return

        payload = {
            "model":       "local",
            "messages":    messages,
            "temperature": temperature,
            "max_tokens":  max_tokens,
            "top_p":       float(os.getenv("TOP_P", "0.95")),
            "stop":        ["</s>", "<|eot_id|>", "<|end|>"],
            "stream":      True,
        }
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", f"{self._base}/v1/chat/completions", json=payload) as resp:
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw == "[DONE]":
                            break
                        chunk = json.loads(raw)
                        token = chunk["choices"][0].get("delta", {}).get("content", "")
                        if token:
                            yield token
        except Exception as e:
            yield f"\n[Error: {e}]"
