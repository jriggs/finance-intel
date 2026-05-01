"""Tests for prompt template detection and building."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from prompts import build_prompt, detect_template

# ── detect_template ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename,expected", [
    ("qwen2.5-14b-instruct-q4.gguf",          "chatml"),
    ("Qwen2.5-14B-Instruct-Q8.gguf",          "chatml"),
    ("hermes-3-llama-3.1-8b.gguf",            "chatml"),
    ("internlm2-chat-7b.gguf",                "chatml"),
    ("Meta-Llama-3.1-8B-Instruct.gguf",       "llama3"),
    ("llama-3-8b-instruct.gguf",              "llama3"),
    ("llama3-70b.gguf",                       "llama3"),
    ("Phi-3-mini-4k-instruct.gguf",           "phi3"),
    ("phi3.5-mini.gguf",                      "phi3"),
    ("gemma-3-4b-it.gguf",                    "gemma"),
    ("gemma-4-27b.gguf",                      "gemma"),
    ("mistral-7b-instruct-v0.2.gguf",         "mistral"),
    ("mixtral-8x7b-instruct.gguf",            "mistral"),
    ("some-random-model-q4_k_m.gguf",         "mistral"),
])
def test_detect_template(filename, expected):
    assert detect_template(f"/models/{filename}") == expected


# ── build_prompt ───────────────────────────────────────────────────────────────

class Msg:
    """Minimal Message stub."""
    def __init__(self, role, content):
        self.role = role
        self.content = content


SYS = "You are helpful."
MSGS = [Msg("user", "Hello"), Msg("assistant", "Hi!"), Msg("user", "How are you?")]


def test_build_prompt_mistral_contains_inst():
    p = build_prompt(SYS, MSGS, "mistral")
    assert "[INST]" in p
    assert "[/INST]" in p
    assert SYS in p
    assert "Hello" in p
    assert "Hi!" in p


def test_build_prompt_chatml_structure():
    p = build_prompt(SYS, MSGS, "chatml")
    assert "<|im_start|>system" in p
    assert "<|im_end|>" in p
    assert "<|im_start|>assistant" in p
    assert p.endswith("<|im_start|>assistant\n")


def test_build_prompt_llama3_structure():
    p = build_prompt(SYS, MSGS, "llama3")
    assert "<|begin_of_text|>" in p
    assert "<|start_header_id|>system<|end_header_id|>" in p
    assert "<|eot_id|>" in p
    assert p.endswith("<|start_header_id|>assistant<|end_header_id|>\n\n")


def test_build_prompt_phi3_structure():
    p = build_prompt(SYS, MSGS, "phi3")
    assert "<|system|>" in p
    assert "<|end|>" in p
    assert "<|user|>" in p
    assert p.endswith("<|assistant|>\n")


def test_build_prompt_gemma_structure():
    p = build_prompt(SYS, MSGS, "gemma")
    assert "<bos>" in p
    assert "<start_of_turn>system" in p
    assert "<start_of_turn>user" in p
    assert "<start_of_turn>model" in p
    assert "<end_of_turn>" in p
    assert p.endswith("<start_of_turn>model\n")


def test_build_prompt_unknown_template_falls_back_to_mistral():
    p = build_prompt(SYS, MSGS, "nonexistent_template")
    assert "[INST]" in p


def test_build_prompt_empty_system():
    msgs = [Msg("user", "Hello")]
    p = build_prompt("", msgs, "chatml")
    assert "Hello" in p


def test_build_prompt_single_user_message():
    msgs = [Msg("user", "What is 2+2?")]
    for template in ("mistral", "chatml", "llama3", "phi3", "gemma"):
        p = build_prompt(SYS, msgs, template)
        assert "What is 2+2?" in p


def test_build_prompt_system_injected_into_first_user_turn_mistral():
    """Mistral injects system into the first user turn, not a separate block."""
    msgs = [Msg("user", "Hello")]
    p = build_prompt("BE HELPFUL", msgs, "mistral")
    # System and user content both appear inside the first [INST]...[/INST]
    inst_content = p.split("[INST]")[1].split("[/INST]")[0]
    assert "BE HELPFUL" in inst_content
    assert "Hello" in inst_content
