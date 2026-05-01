"""
Dataset Preparation — converts various input formats into MLX-compatible JSONL.

Supported input formats:
  1. JSONL with {"prompt": "...", "completion": "..."}   (Q&A pairs)
  2. JSONL with {"messages": [...]}                       (chat format)
  3. JSONL with {"text": "..."}                           (raw text, used as-is)
  4. Plain .txt files                                     (chunked into training examples)
  5. CSV with "prompt" and "completion" columns

Output: train.jsonl + valid.jsonl in the target dataset directory,
formatted for mlx_lm with Mistral [INST] prompt template.

Usage:
  python prepare_data.py --input ./raw/my_data.jsonl --output ./datasets/my_data
  python prepare_data.py --input ./raw/docs.txt --output ./datasets/docs --chunk-size 512
"""

import argparse
import csv
import json
import random
from pathlib import Path


MISTRAL_TEMPLATE = "[INST] {prompt} [/INST] {completion}"


def format_example(prompt: str, completion: str) -> dict:
    """Wrap a single Q/A pair in Mistral instruct format."""
    text = f"<s>{MISTRAL_TEMPLATE.format(prompt=prompt.strip(), completion=completion.strip())}</s>"
    return {"text": text}


def from_qa_jsonl(path: Path) -> list[dict]:
    examples = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "prompt" in obj and "completion" in obj:
                examples.append(format_example(obj["prompt"], obj["completion"]))
            elif "input" in obj and "output" in obj:
                examples.append(format_example(obj["input"], obj["output"]))
            elif "question" in obj and "answer" in obj:
                examples.append(format_example(obj["question"], obj["answer"]))
            elif "text" in obj:
                # Already formatted raw text — use directly
                examples.append({"text": obj["text"]})
            elif "messages" in obj:
                # Chat format: convert turns to Mistral instruct
                messages = obj["messages"]
                text = "<s>"
                system = ""
                i = 0
                while i < len(messages):
                    msg = messages[i]
                    if msg["role"] == "system":
                        system = msg["content"]
                        i += 1
                        continue
                    if msg["role"] == "user":
                        user_content = msg["content"]
                        if system:
                            user_content = f"{system}\n\n{user_content}"
                            system = ""
                        # Look ahead for assistant response
                        if i + 1 < len(messages) and messages[i + 1]["role"] == "assistant":
                            asst_content = messages[i + 1]["content"]
                            text += f"[INST] {user_content} [/INST] {asst_content}</s>"
                            i += 2
                        else:
                            i += 1
                    else:
                        i += 1
                if text != "<s>":
                    examples.append({"text": text})
    return examples


def from_csv(path: Path) -> list[dict]:
    examples = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            prompt = row.get("prompt") or row.get("input") or row.get("question", "")
            completion = row.get("completion") or row.get("output") or row.get("answer", "")
            if prompt and completion:
                examples.append(format_example(prompt, completion))
    return examples


def from_txt(path: Path, chunk_size: int = 512) -> list[dict]:
    """Chunk plain text into overlapping training examples."""
    text = path.read_text(encoding="utf-8")
    words = text.split()
    examples = []
    step = chunk_size - 64  # 64-word overlap
    for i in range(0, len(words), step):
        chunk = " ".join(words[i : i + chunk_size])
        if len(chunk.strip()) < 50:
            continue
        examples.append({"text": chunk})
    return examples


def load_examples(input_path: Path, chunk_size: int) -> list[dict]:
    suffix = input_path.suffix.lower()
    if suffix == ".jsonl" or suffix == ".json":
        return from_qa_jsonl(input_path)
    elif suffix == ".csv":
        return from_csv(input_path)
    elif suffix == ".txt":
        return from_txt(input_path, chunk_size)
    else:
        raise ValueError(f"Unsupported file type: {suffix}. Use .jsonl, .csv, or .txt")


def main():
    parser = argparse.ArgumentParser(description="Prepare training dataset for MLX LoRA")
    parser.add_argument("--input", required=True,
                        help="Input file (.jsonl, .csv, or .txt)")
    parser.add_argument("--output", required=True,
                        help="Output directory for train.jsonl and valid.jsonl")
    parser.add_argument("--val-split", type=float, default=0.1,
                        help="Fraction of data to use for validation (default 0.1)")
    parser.add_argument("--chunk-size", type=int, default=512,
                        help="Words per chunk for .txt files")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading {input_path}…")
    examples = load_examples(input_path, args.chunk_size)

    if not examples:
        print("❌ No examples found. Check your input file format.")
        return

    # Shuffle and split
    random.seed(args.seed)
    random.shuffle(examples)
    val_n = max(1, int(len(examples) * args.val_split))
    train_examples = examples[val_n:]
    val_examples = examples[:val_n]

    # Write JSONL
    train_path = output_dir / "train.jsonl"
    valid_path = output_dir / "valid.jsonl"

    with open(train_path, "w") as f:
        for ex in train_examples:
            f.write(json.dumps(ex) + "\n")

    with open(valid_path, "w") as f:
        for ex in val_examples:
            f.write(json.dumps(ex) + "\n")

    print(f"✅ Dataset prepared:")
    print(f"   Train: {len(train_examples)} examples → {train_path}")
    print(f"   Valid: {len(val_examples)} examples → {valid_path}")
    print(f"\nNext step:")
    print(f"   python train.py --model mistralai/Mistral-7B-Instruct-v0.2 --data {output_dir}")


if __name__ == "__main__":
    main()
