"""
Export — merges LoRA adapters into the base model and converts to GGUF.

Step 1: Fuse adapters into the base model weights (MLX → HuggingFace format)
Step 2: Convert the fused HuggingFace model to GGUF using llama.cpp

Usage:
  python export.py --model mistralai/Mistral-7B-Instruct-v0.2 \
                   --adapters ./adapters \
                   --output ../models/mistral-7b-finetuned.gguf \
                   --quantize q4_k_m

Requirements:
  pip install mlx-lm
  brew install llvm  (for llama.cpp convert script, only if not already present)
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def fuse_adapters(model_id: str, adapter_path: Path, fused_dir: Path):
    """Merge LoRA adapters into the base model using mlx_lm.fuse."""
    print(f"Fusing adapters from {adapter_path} into {model_id}…")
    try:
        from mlx_lm import fuse as mlx_fuse
    except ImportError:
        print("❌ mlx-lm not installed. Run: pip install mlx-lm")
        sys.exit(1)

    fused_dir.mkdir(parents=True, exist_ok=True)

    old_argv = sys.argv
    sys.argv = [
        "mlx_lm.fuse",
        "--model", model_id,
        "--adapter-path", str(adapter_path),
        "--save-path", str(fused_dir),
        "--de-quantize",   # convert back to float16 for GGUF conversion
    ]
    mlx_fuse.main()
    sys.argv = old_argv
    print(f"✅ Fused model saved to: {fused_dir}")


def convert_to_gguf(fused_dir: Path, output_gguf: Path, quantize: str):
    """Convert HuggingFace model to GGUF using llama.cpp convert script."""

    # Try to find llama.cpp convert script
    convert_script = None
    candidates = [
        Path.home() / "llama.cpp" / "convert_hf_to_gguf.py",
        Path.home() / "llama.cpp" / "convert-hf-to-gguf.py",
        Path("/opt/homebrew/share/llama.cpp/convert_hf_to_gguf.py"),
    ]
    for c in candidates:
        if c.exists():
            convert_script = c
            break

    if convert_script is None:
        print("\n⚠️  llama.cpp convert script not found.")
        print("   Install llama.cpp:")
        print("     brew install llama.cpp")
        print("   Or clone it:")
        print("     git clone https://github.com/ggerganov/llama.cpp ~/llama.cpp")
        print("     pip install -r ~/llama.cpp/requirements.txt")
        print(f"\n   Then run manually:")
        print(f"     python ~/llama.cpp/convert_hf_to_gguf.py {fused_dir} --outfile {output_gguf}.f16.gguf")
        print(f"     llama-quantize {output_gguf}.f16.gguf {output_gguf} {quantize.upper()}")
        return False

    print(f"\nConverting to GGUF (f16)…")
    f16_path = output_gguf.parent / (output_gguf.stem + ".f16.gguf")

    result = subprocess.run(
        [sys.executable, str(convert_script), str(fused_dir),
         "--outfile", str(f16_path), "--outtype", "f16"],
        capture_output=False,
    )
    if result.returncode != 0:
        print("❌ GGUF conversion failed.")
        return False

    print(f"\nQuantizing to {quantize.upper()}…")
    # Find llama-quantize binary
    quantize_bin = shutil.which("llama-quantize")
    if quantize_bin is None:
        print("⚠️  llama-quantize not found in PATH.")
        print(f"   f16 GGUF is at: {f16_path}")
        print(f"   Manually quantize: llama-quantize {f16_path} {output_gguf} {quantize.upper()}")
        return True

    result = subprocess.run(
        [quantize_bin, str(f16_path), str(output_gguf), quantize.upper()],
        capture_output=False,
    )
    if result.returncode != 0:
        print("❌ Quantization failed.")
        return False

    # Clean up f16
    f16_path.unlink(missing_ok=True)
    print(f"\n✅ Exported: {output_gguf}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Merge LoRA adapters and export to GGUF")
    parser.add_argument("--model", required=True,
                        help="Base HuggingFace model ID used during training")
    parser.add_argument("--adapters", default="./adapters",
                        help="Path to adapter directory from train.py")
    parser.add_argument("--output", default="../models/finetuned.gguf",
                        help="Output path for the final GGUF file")
    parser.add_argument("--quantize", default="q4_k_m",
                        choices=["q4_k_m", "q5_k_m", "q8_0", "f16"],
                        help="Quantization type for the output GGUF")
    parser.add_argument("--keep-fused", action="store_true",
                        help="Keep the intermediate fused HuggingFace model")
    args = parser.parse_args()

    adapter_path = Path(args.adapters)
    output_gguf = Path(args.output)
    output_gguf.parent.mkdir(parents=True, exist_ok=True)
    fused_dir = output_gguf.parent / (output_gguf.stem + "_fused_hf")

    if not adapter_path.exists():
        print(f"❌ Adapter path not found: {adapter_path}")
        sys.exit(1)

    # Step 1: Fuse adapters
    fuse_adapters(args.model, adapter_path, fused_dir)

    # Step 2: Convert to GGUF
    if args.quantize != "skip":
        success = convert_to_gguf(fused_dir, output_gguf, args.quantize)

    # Cleanup
    if not args.keep_fused and fused_dir.exists():
        shutil.rmtree(fused_dir)
        print(f"Cleaned up intermediate fused model.")

    print(f"\n📦 Done! Update your .env:")
    print(f"   MODEL_PATH={output_gguf.resolve()}")


if __name__ == "__main__":
    main()
