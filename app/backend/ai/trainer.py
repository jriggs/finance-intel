"""
Trainer — manages LoRA fine-tuning jobs launched as subprocesses.
Reads progress from train/training_status.json and train/training_log.jsonl.
"""

import contextlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TRAIN_DIR    = Path(__file__).parent.parent / "train"
DATASETS_DIR = TRAIN_DIR / "datasets"
ADAPTERS_DIR = TRAIN_DIR / "adapters"
STATUS_FILE  = TRAIN_DIR / "training_status.json"
LOG_FILE     = TRAIN_DIR / "training_log.jsonl"


class TrainerManager:
    """
    Manages a single LoRA training subprocess.
    All mutable state is encapsulated here — no module-level globals needed.
    """

    def __init__(self):
        self._process: subprocess.Popen | None = None

    # ── Status & logs ──────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        if STATUS_FILE.exists():
            try:
                return json.loads(STATUS_FILE.read_text())
            except Exception:
                pass
        return {"status": "idle"}

    def get_log(self, last_n: int = 50) -> list:
        if not LOG_FILE.exists():
            return []
        lines = []
        try:
            with open(LOG_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        with contextlib.suppress(Exception):
                            lines.append(json.loads(line))
        except Exception:
            pass
        return lines[-last_n:]

    # ── Dataset / adapter discovery ────────────────────────────────────────────

    def list_datasets(self) -> list:
        DATASETS_DIR.mkdir(parents=True, exist_ok=True)
        datasets = []
        for d in sorted(DATASETS_DIR.iterdir()):
            if not d.is_dir():
                continue
            datasets.append({
                "name": d.name,
                "path": str(d),
                "train_examples": _count_lines(d / "train.jsonl"),
                "valid_examples": _count_lines(d / "valid.jsonl"),
                "created": datetime.fromtimestamp(d.stat().st_ctime).isoformat(),
            })
        return datasets

    def list_adapters(self) -> list:
        ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
        adapters = []
        for d in sorted(ADAPTERS_DIR.iterdir()):
            if not d.is_dir():
                continue
            adapter_file = d / "adapters.npz"
            if adapter_file.exists():
                size_mb = adapter_file.stat().st_size / 1_000_000
                adapters.append({
                    "name": d.name,
                    "path": str(d),
                    "size_mb": round(size_mb, 1),
                    "created": datetime.fromtimestamp(d.stat().st_ctime).isoformat(),
                })
        return adapters

    # ── Training lifecycle ─────────────────────────────────────────────────────

    def start_training(
        self,
        dataset_name: str,
        model_id: str = "mistralai/Mistral-7B-Instruct-v0.2",
        iters: int = 500,
        batch_size: int = 4,
        learning_rate: float = 1e-4,
        lora_rank: int = 8,
        lora_layers: int = 16,
        max_seq_len: int = 2048,
    ) -> dict:
        status = self.get_status()
        if status.get("status") in ("training", "downloading", "starting"):
            return {"error": "A training job is already running."}

        dataset_path = DATASETS_DIR / dataset_name
        if not dataset_path.exists():
            return {"error": f"Dataset '{dataset_name}' not found."}

        adapter_out = ADAPTERS_DIR / dataset_name
        adapter_out.mkdir(parents=True, exist_ok=True)

        # Clear old status/log
        for f in (STATUS_FILE, LOG_FILE):
            if f.exists():
                f.unlink()

        cmd = [
            sys.executable,
            str(TRAIN_DIR / "train.py"),
            "--model",          model_id,
            "--data",           str(dataset_path),
            "--output",         str(adapter_out),
            "--iters",          str(iters),
            "--batch-size",     str(batch_size),
            "--learning-rate",  str(learning_rate),
            "--lora-rank",      str(lora_rank),
            "--lora-layers",    str(lora_layers),
            "--max-seq-len",    str(max_seq_len),
        ]

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        return {
            "status":  "started",
            "pid":     self._process.pid,
            "dataset": dataset_name,
            "model":   model_id,
            "iters":   iters,
        }

    def stop_training(self) -> dict:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            self._process = None
            STATUS_FILE.write_text(json.dumps({"status": "stopped"}))
            return {"status": "stopped"}
        return {"status": "no job running"}

    def save_dataset_file(self, name: str, content: str, fmt: str) -> dict:
        """Save uploaded training data and convert it to a train/valid split."""
        if not name or any(c in name for c in ("/", "\\", "..")) or name.startswith("."):
            return {"error": "Invalid dataset name"}
        DATASETS_DIR.mkdir(parents=True, exist_ok=True)

        raw_dir = DATASETS_DIR / "_raw"
        raw_dir.mkdir(exist_ok=True)
        raw_file = raw_dir / f"{name}.{fmt}"
        raw_file.write_text(content, encoding="utf-8")

        out_dir = DATASETS_DIR / name
        result = subprocess.run(
            [
                sys.executable,
                str(TRAIN_DIR / "prepare_data.py"),
                "--input",  str(raw_file),
                "--output", str(out_dir),
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return {"error": result.stderr or result.stdout}

        return {
            "name":           name,
            "train_examples": _count_lines(out_dir / "train.jsonl"),
            "valid_examples": _count_lines(out_dir / "valid.jsonl"),
        }


# ── Module-level singleton ─────────────────────────────────────────────────────
# All imports in main.py use these thin wrappers — the public API is unchanged.

_manager = TrainerManager()


def get_status() -> dict:
    return _manager.get_status()


def get_log(last_n: int = 50) -> list:
    return _manager.get_log(last_n)


def list_datasets() -> list:
    return _manager.list_datasets()


def list_adapters() -> list:
    return _manager.list_adapters()


def start_training(**kwargs) -> dict:
    return _manager.start_training(**kwargs)


def stop_training() -> dict:
    return _manager.stop_training()


def save_dataset_file(name: str, content: str, fmt: str) -> dict:
    return _manager.save_dataset_file(name, content, fmt)


# ── Utilities ──────────────────────────────────────────────────────────────────

def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with open(path) as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0
