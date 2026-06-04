"""
Prompt Template Builders

Provides detect_template() and build_prompt() for all supported model families.

To add a new template:
  1. Write a _prompt_<name>(system, messages) -> str function below.
  2. Register it in _BUILDERS.
  3. Add its detection keywords to _TEMPLATE_MAP.
  No other code needs to change (OCP).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

# (template_name, filename_keywords) — evaluated in order; first match wins.
_TEMPLATE_MAP: list[tuple[str, list[str]]] = [
    ("chatml",  ["qwen", "chatml", "internlm", "hermes"]),
    ("llama3",  ["llama-3", "llama3", "meta-llama-3"]),
    ("phi3",    ["phi-3", "phi3"]),
    ("gemma",   ["gemma"]),
]
_DEFAULT_TEMPLATE = "mistral"


def detect_template(model_path: str) -> str:
    """Infer the prompt template from the model filename."""
    name = Path(model_path).name.lower()
    for template, keywords in _TEMPLATE_MAP:
        if any(k in name for k in keywords):
            return template
    return _DEFAULT_TEMPLATE


def build_prompt(system: str, messages: list, template: str = "mistral") -> str:
    """Dispatch to the correct prompt builder for the loaded model."""
    return _BUILDERS.get(template, _prompt_mistral)(system, messages)


# ── Template implementations ───────────────────────────────────────────────────

def _prompt_mistral(system: str, messages: list) -> str:
    """Mistral / Mixtral — [INST] format. System injected into first user turn."""
    prompt = "<s>"
    first_user = True
    for msg in messages:
        if msg.role == "user":
            content = f"{system}\n\n{msg.content}" if first_user and system else msg.content
            first_user = False
            prompt += f"[INST] {content} [/INST]"
        elif msg.role == "assistant":
            prompt += f" {msg.content}</s>"
    return prompt


def _prompt_chatml(system: str, messages: list) -> str:
    """ChatML — Qwen2.5, InternLM, Hermes, and most modern models."""
    prompt = f"<|im_start|>system\n{system}<|im_end|>\n"
    for msg in messages:
        prompt += f"<|im_start|>{msg.role}\n{msg.content}<|im_end|>\n"
    prompt += "<|im_start|>assistant\n"
    return prompt


def _prompt_llama3(system: str, messages: list) -> str:
    """Llama 3 / Llama 3.1 / Llama 3.2 format."""
    prompt = "<|begin_of_text|>"
    prompt += f"<|start_header_id|>system<|end_header_id|>\n\n{system}<|eot_id|>"
    for msg in messages:
        prompt += f"<|start_header_id|>{msg.role}<|end_header_id|>\n\n{msg.content}<|eot_id|>"
    prompt += "<|start_header_id|>assistant<|end_header_id|>\n\n"
    return prompt


def _prompt_phi3(system: str, messages: list) -> str:
    """Phi-3 / Phi-3.5 format."""
    prompt = f"<|system|>\n{system}<|end|>\n"
    for msg in messages:
        tag = "user" if msg.role == "user" else "assistant"
        prompt += f"<|{tag}|>\n{msg.content}<|end|>\n"
    prompt += "<|assistant|>\n"
    return prompt


def _prompt_gemma(system: str, messages: list) -> str:
    """Gemma 3 / 4 format — system has its own dedicated turn."""
    prompt = "<bos>"
    if system:
        prompt += f"<start_of_turn>system\n{system}<end_of_turn>\n"
    for msg in messages:
        if msg.role == "user":
            prompt += f"<start_of_turn>user\n{msg.content}<end_of_turn>\n"
        elif msg.role == "assistant":
            prompt += f"<start_of_turn>model\n{msg.content}<end_of_turn>\n"
    prompt += "<start_of_turn>model\n"
    return prompt


# Registry — maps template name → builder function.
# Adding a new template only requires editing this dict + writing the function above.
_BUILDERS: dict[str, Callable] = {
    "mistral": _prompt_mistral,
    "chatml":  _prompt_chatml,
    "llama3":  _prompt_llama3,
    "phi3":    _prompt_phi3,
    "gemma":   _prompt_gemma,
}
