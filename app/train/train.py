"""
LoRA Fine-tuning via MLX — Apple Silicon optimized.

Usage:
  python train.py --model mistralai/Mistral-7B-Instruct-v0.2 \
                  --data ./datasets/my_data \
                  --iters 500

MLX downloads the HuggingFace model automatically on first run.
Trained adapters are saved to ./adapters/ and can be exported to GGUF
with export.py.

Progress is written to ./training_log.jsonl so the API can stream it.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_FILE = Path(__file__).parent / "training_log.jsonl"
STATUS_FILE = Path(__file__).parent / "training_status.json"


def write_status(status: str, **kwargs):
    data = {"status": status, "time": datetime.utcnow().isoformat(), **kwargs}
    STATUS_FILE.write_text(json.dumps(data))


def write_log(entry: dict):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps({**entry, "time": datetime.utcnow().isoformat()}) + "\n")


def main():
    parser = argparse.ArgumentParser(description="MLX LoRA fine-tuning")
    parser.add_argument("--model", required=True,
                        help="HuggingFace model ID (e.g. mistralai/Mistral-7B-Instruct-v0.2)")
    parser.add_argument("--data", required=True,
                        help="Path to dataset directory containing train.jsonl (and optionally valid.jsonl)")
    parser.add_argument("--output", default="./adapters",
                        help="Output directory for adapter weights")
    parser.add_argument("--iters", type=int, default=500,
                        help="Number of training iterations")
    parser.add_argument("--batch-size", type=int, default=4,
                        help="Batch size (reduce if OOM)")
    parser.add_argument("--learning-rate", type=float, default=1e-4,
                        help="Learning rate")
    parser.add_argument("--lora-rank", type=int, default=8,
                        help="LoRA rank (higher = more capacity, more memory)")
    parser.add_argument("--lora-layers", type=int, default=16,
                        help="Number of layers to apply LoRA to (from the top)")
    parser.add_argument("--steps-per-report", type=int, default=10,
                        help="Log loss every N steps")
    parser.add_argument("--steps-per-eval", type=int, default=100,
                        help="Evaluate on validation set every N steps")
    parser.add_argument("--save-every", type=int, default=100,
                        help="Save adapter checkpoint every N steps")
    parser.add_argument("--max-seq-len", type=int, default=2048,
                        help="Maximum sequence length for training")
    args = parser.parse_args()

    # Clear old log
    if LOG_FILE.exists():
        LOG_FILE.unlink()

    write_status("starting", model=args.model, iters=args.iters)
    write_log({"event": "start", "model": args.model, "iters": args.iters,
               "lr": args.learning_rate, "lora_rank": args.lora_rank})

    try:
        from mlx_lm import lora as mlx_lora
    except ImportError:
        msg = "mlx-lm not installed. Run: pip install mlx-lm"
        write_status("error", message=msg)
        write_log({"event": "error", "message": msg})
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = Path(args.data)

    if not (data_dir / "train.jsonl").exists():
        msg = f"train.jsonl not found in {data_dir}. Run prepare_data.py first."
        write_status("error", message=msg)
        write_log({"event": "error", "message": msg})
        sys.exit(1)

    write_status("downloading", model=args.model)
    write_log({"event": "info", "message": f"Loading model {args.model} (downloading if needed)…"})

    # Build mlx_lm.lora arguments
    mlx_args = [
        "--model", args.model,
        "--train",
        "--data", str(data_dir),
        "--output-dir", str(output_dir),
        "--iters", str(args.iters),
        "--batch-size", str(args.batch_size),
        "--learning-rate", str(args.learning_rate),
        "--lora-rank", str(args.lora_rank),
        "--num-layers", str(args.lora_layers),
        "--steps-per-report", str(args.steps_per_report),
        "--steps-per-eval", str(args.steps_per_eval),
        "--save-every", str(args.save_every),
        "--max-seq-length", str(args.max_seq_len),
        "--adapter-path", str(output_dir),
    ]

    write_status("training", model=args.model, iters=args.iters, current_iter=0)

    # Monkey-patch stdout to capture mlx_lm progress lines and relay them
    import io
    import threading

    original_stdout = sys.stdout

    class ProgressCapture(io.TextIOBase):
        def write(self, s):
            original_stdout.write(s)
            original_len = len(s)
            s = s.strip()
            if not s:
                return original_len
            # Parse mlx_lm progress lines: "Iter N: Train loss X.XXX, ..."
            if s.startswith("Iter "):
                try:
                    parts = s.split(",")
                    iter_part = parts[0]  # "Iter N: Train loss X.XXX"
                    iter_num = int(iter_part.split(":")[0].replace("Iter", "").strip())
                    loss_val = float(iter_part.split("loss")[-1].strip())
                    write_status("training", current_iter=iter_num,
                                 total_iters=args.iters, loss=loss_val,
                                 progress=round(iter_num / args.iters * 100, 1))
                    write_log({"event": "progress", "iter": iter_num,
                               "loss": loss_val, "message": s})
                except Exception:
                    write_log({"event": "info", "message": s})
            elif "Saved" in s or "saved" in s:
                write_log({"event": "checkpoint", "message": s})
            elif "Val loss" in s or "Validation" in s:
                write_log({"event": "eval", "message": s})
            else:
                write_log({"event": "info", "message": s})
            return original_len

        def flush(self):
            original_stdout.flush()

    sys.stdout = ProgressCapture()

    try:
        # Invoke mlx_lm.lora via its main() with our args
        import sys as _sys
        _sys.argv = ["mlx_lm.lora"] + mlx_args
        mlx_lora.main()
        sys.stdout = original_stdout
        write_status("done", output=str(output_dir),
                     message="Training complete! Run export.py to merge and convert to GGUF.")
        write_log({"event": "done", "output": str(output_dir)})
        print(f"\n✅ Training complete. Adapters saved to: {output_dir}")
    except Exception as e:
        sys.stdout = original_stdout
        write_status("error", message=str(e))
        write_log({"event": "error", "message": str(e)})
        raise


if __name__ == "__main__":
    main()
