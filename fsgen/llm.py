"""LLM backends: Anthropic SDK (API key / ant profile) or the local `claude` CLI."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(RuntimeError):
    pass


def extract_code(text: str) -> str | None:
    blocks = re.findall(r"```(?:featurescript|fs|javascript|js)?\s*\n(.*?)```", text, re.S)
    blocks = [b for b in blocks if "defineFeature" in b]
    return blocks[-1].strip() + "\n" if blocks else None


def _merge_images(image, images) -> list[Path]:
    return ([Path(image)] if image else []) + [Path(i) for i in images or ()]


def image_media_type(data: bytes) -> str:
    """Media type from magic bytes (png/jpeg/webp/gif); png when unknown."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "high"):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model, self.effort = model, effort

    def complete(self, system: str, prompt: str, image: Path | None = None, images: Sequence[Path] = ()) -> str:
        content: list = []
        for img in _merge_images(image, images):
            data = img.read_bytes()
            content.append({"type": "image", "source": {"type": "base64", "media_type": image_media_type(data),
                                                        "data": base64.standard_b64encode(data).decode()}})
        content.append({"type": "text", "text": prompt})
        # The system prompt (spec + examples) is identical across calls, so cache it.
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=64000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise LLMError(f"model declined: {getattr(msg.stop_details, 'explanation', '')}")
        return "".join(b.text for b in msg.content if b.type == "text")


class ClaudeCLI:
    """Uses the logged-in Claude Code CLI; no API key needed. Images are passed by path; the CLI opens
    each one with its Read tool (allowed per file)."""

    name = "claude-cli"

    def __init__(self, model: str | None = None, timeout: int | None = None, web: bool = True):
        if not shutil.which("claude"):
            raise LLMError("`claude` CLI not found")
        self.model, self.web = model, web
        self.timeout = timeout or int(os.environ.get("FSGEN_LLM_TIMEOUT", 1800))

    def complete(self, system: str, prompt: str, image: Path | None = None, images: Sequence[Path] = ()) -> str:
        imgs = _merge_images(image, images)
        tools, allowed = [], []
        if self.web:
            # product dimensions etc. can be looked up like in a chat (uses Claude usage, not Onshape calls)
            tools += ["WebSearch", "WebFetch"]
            allowed += ["WebSearch", "WebFetch"]
        if imgs:
            # Let the CLI look at the images with its Read tool (read-only, these files only).
            tools.append("Read")
            allowed += [f"Read({i.resolve()})" for i in imgs]
            paths = ", ".join(str(i.resolve()) for i in imgs)
            prompt = f"Images for this request (open each with the Read tool): {paths}\n\n{prompt}"
        cmd = ["claude", "-p", "--tools", ",".join(tools), "--no-session-persistence", "--output-format", "text",
               "--system-prompt", system]
        if allowed:
            cmd += ["--allowedTools", *allowed]
        if self.model:
            cmd += ["--model", self.model]
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise LLMError(f"claude CLI did not answer within {self.timeout}s (set FSGEN_LLM_TIMEOUT to allow more)")
        if r.returncode != 0:
            raise LLMError(f"claude CLI failed ({r.returncode}): {r.stderr[-2000:]}")
        return r.stdout


class CommandLLM:
    """Any LLM with a command-line interface: the prompt goes to stdin, the answer comes from stdout.

    Set FSGEN_LLM_COMMAND (or --backend command --llm-command ...), e.g.
        ollama run qwen2.5-coder:32b
        gemini -p
        codex exec -
        llm -m gpt-4o            (Simon Willison's `llm` tool, many providers)
    The system prompt (spec, rules, examples) is sent first in the same input. Images are not sent;
    their paths are mentioned so tools that can read files may use it.
    """

    name = "command"

    def __init__(self, command: str, timeout: int | None = None):
        import shlex
        if not command:
            raise LLMError("no LLM command: set FSGEN_LLM_COMMAND, e.g. 'ollama run qwen2.5-coder:32b'")
        self.argv = shlex.split(command)
        if not shutil.which(self.argv[0]):
            raise LLMError(f"LLM command not found: {self.argv[0]}")
        self.name = f"command ({self.argv[0]})"
        self.timeout = timeout or int(os.environ.get("FSGEN_LLM_TIMEOUT", 1800))

    def complete(self, system: str, prompt: str, image: Path | None = None, images: Sequence[Path] = ()) -> str:
        text = f"{system}\n\n---\n\n{prompt}"
        imgs = _merge_images(image, images)
        if imgs:
            text += "\n\n(Images for this request are at: " + ", ".join(str(i.resolve()) for i in imgs) + ".)"
        try:
            r = subprocess.run(self.argv, input=text, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise LLMError(f"{self.argv[0]} did not answer within {self.timeout}s (FSGEN_LLM_TIMEOUT)")
        if r.returncode != 0:
            raise LLMError(f"{self.argv[0]} failed ({r.returncode}): {r.stderr[-2000:]}")
        return r.stdout


DEFAULT_OLLAMA_MODEL = "gemma4:12b-it-qat"


def ollama_host() -> str:
    h = os.environ.get("OLLAMA_HOST", "").strip() or "http://localhost:11434"
    if "://" not in h:
        h = "http://" + h
    return h.rstrip("/")


def _ollama_model(model: str | None) -> str:
    return model or os.environ.get("FSGEN_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL


def ollama_status(timeout: float = 2) -> dict:
    """{"available", "model", "models"}; never raises. available = the configured model is installed."""
    import urllib.request
    model = _ollama_model(None)
    try:
        with urllib.request.urlopen(f"{ollama_host()}/api/tags", timeout=timeout) as r:
            data = json.loads(r.read())
        names = [m.get("name") or m.get("model") for m in data.get("models", []) if isinstance(m, dict)]
        names = [n for n in names if isinstance(n, str)]
    except Exception:  # noqa: BLE001
        return {"available": False, "model": model, "models": []}
    ok = model in names or (":" not in model and f"{model}:latest" in names)
    return {"available": ok, "model": model, "models": names}


class OllamaLLM:
    """Local models through the Ollama HTTP API (text and images). No extra dependencies."""

    name = "ollama"

    def __init__(self, model: str | None = None, timeout: int | None = None):
        self.model = _ollama_model(model)
        self.host = ollama_host()
        self.timeout = timeout or int(os.environ.get("FSGEN_LLM_TIMEOUT", 1800))

    def complete(self, system: str, prompt: str, image: Path | None = None, images: Sequence[Path] = ()) -> str:
        import urllib.error
        import urllib.request
        user: dict = {"role": "user", "content": prompt}
        imgs = _merge_images(image, images)
        if imgs:
            user["images"] = [base64.b64encode(i.read_bytes()).decode() for i in imgs]
        body = {"model": self.model, "stream": False, "options": {"temperature": 0.2},
                "messages": [{"role": "system", "content": system}, user]}
        # Thinking models (gemma4, qwen3) think by default: several times slower, and a long think can leave the
        # answer empty. Off unless FSGEN_OLLAMA_THINK=1.
        if os.environ.get("FSGEN_OLLAMA_THINK", "").strip().lower() not in ("1", "true", "yes"):
            body["think"] = False
        req = urllib.request.Request(f"{self.host}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code == 404:
                raise LLMError(f"Ollama model {self.model!r} not found; run `ollama pull {self.model}` ({detail})")
            raise LLMError(f"Ollama error {e.code}: {detail}")
        except TimeoutError:
            raise LLMError(f"Ollama did not answer within {self.timeout}s (FSGEN_LLM_TIMEOUT)")
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"cannot reach Ollama at {self.host} ({getattr(e, 'reason', e)}); is it running? "
                           "(OLLAMA_HOST)")
        except ValueError:
            raise LLMError("Ollama returned an invalid response")
        if isinstance(data, dict) and data.get("error"):
            raise LLMError(f"Ollama: {data['error']}")
        text = (data.get("message") or {}).get("content") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise LLMError("Ollama returned an empty answer")
        return text


def make_llm(backend: str = "auto", model: str | None = None, web: bool = True, command: str | None = None):
    command = command or os.environ.get("FSGEN_LLM_COMMAND")
    if backend == "command" or (backend == "auto" and command):
        return CommandLLM(command or "")
    if backend == "ollama":
        return OllamaLLM(model)
    if backend == "anthropic" or (backend == "auto" and _has_anthropic_key()):
        return AnthropicLLM(model or DEFAULT_MODEL)
    return ClaudeCLI(model, web=web)


def _has_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or Path("~/.config/anthropic").expanduser().exists())


def claude_llm(model: str | None = None, web: bool = True):
    """Claude only (API if a key is set, else the claude CLI), ignoring FSGEN_LLM_COMMAND; both can see images."""
    return AnthropicLLM(model or DEFAULT_MODEL) if _has_anthropic_key() else ClaudeCLI(model, web=web)
