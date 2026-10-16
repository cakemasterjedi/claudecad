"""LLM backends: image handling, Ollama request, CLI tool permissions (no network, no subprocess)."""
import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from fsgen import llm
from fsgen.native import prompts

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8


def test_media_types():
    assert llm.image_media_type(PNG) == "image/png"
    assert llm.image_media_type(b"\xff\xd8\xff\xe0abc") == "image/jpeg"
    assert llm.image_media_type(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert llm.image_media_type(b"GIF89a....") == "image/gif"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_ollama_request(monkeypatch, tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(PNG)
    b.write_bytes(PNG + b"x")
    monkeypatch.setenv("OLLAMA_HOST", "box:1234")
    monkeypatch.delenv("FSGEN_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("FSGEN_OLLAMA_THINK", raising=False)
    got = {}

    def urlopen(req, timeout=None):
        got.update(url=req.full_url, body=json.loads(req.data), timeout=timeout)
        return _Resp(json.dumps({"message": {"content": "hi"}}).encode())
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert llm.OllamaLLM().complete("sys", "usr", image=a, images=[b]) == "hi"
    body = got["body"]
    assert got["url"] == "http://box:1234/api/chat"
    assert body["model"] == "gemma4:12b-it-qat" and body["stream"] is False
    assert body["options"] == {"temperature": 0.2} and body["think"] is False
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1]["content"] == "usr" and len(body["messages"][1]["images"]) == 2


def test_ollama_errors(monkeypatch):
    def refuse(req, timeout=None):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(llm.LLMError, match="cannot reach Ollama"):
        llm.OllamaLLM().complete("s", "p")

    def missing(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b'{"error":"model not found"}'))
    monkeypatch.setattr("urllib.request.urlopen", missing)
    with pytest.raises(llm.LLMError, match="ollama pull"):
        llm.OllamaLLM("nope").complete("s", "p")

    def empty(req, timeout=None):  # thinking used the whole budget
        return _Resp(json.dumps({"message": {"content": "", "thinking": "..."}}).encode())
    monkeypatch.setattr("urllib.request.urlopen", empty)
    with pytest.raises(llm.LLMError, match="empty answer"):
        llm.OllamaLLM().complete("s", "p")


def test_ollama_status(monkeypatch):
    monkeypatch.setenv("FSGEN_OLLAMA_MODEL", "m1")
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Resp(json.dumps({"models": [{"name": "m1"}, {"name": "m2"}]}).encode()))
    assert llm.ollama_status() == {"available": True, "model": "m1", "models": ["m1", "m2"]}
    monkeypatch.setenv("FSGEN_OLLAMA_MODEL", "other")
    assert llm.ollama_status()["available"] is False

    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert llm.ollama_status() == {"available": False, "model": "other", "models": []}


def test_claude_cli_allows_read_per_image(monkeypatch, tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(PNG)
    b.write_bytes(PNG)
    monkeypatch.setattr(llm.shutil, "which", lambda n: "/usr/bin/claude")
    seen = {}

    def run(cmd, **k):
        seen.update(cmd=cmd, input=k["input"])
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    monkeypatch.setattr(llm.subprocess, "run", run)
    assert llm.ClaudeCLI(web=False).complete("s", "p", image=a, images=[b]) == "ok"
    cmd = seen["cmd"]
    assert f"Read({a.resolve()})" in cmd and f"Read({b.resolve()})" in cmd
    assert "Images for this request (open each with the Read tool)" in seen["input"]


def test_make_llm_ollama():
    o = llm.make_llm("ollama", "x:1")
    assert isinstance(o, llm.OllamaLLM) and o.model == "x:1" and o.name == "ollama"


def test_measure_prompt():
    system, p = prompts.measure_prompt("units are inches")
    assert "(estimated)" in p and "(unsure)" in p and "units are inches" in p
    assert "units are" not in prompts.measure_prompt()[1]


def test_claude_llm_ignores_llm_command(monkeypatch):
    from fsgen import llm
    monkeypatch.setenv("FSGEN_LLM_COMMAND", "ollama run x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(llm, "_has_anthropic_key", lambda: False)
    assert isinstance(llm.make_llm("auto"), llm.CommandLLM)
    assert isinstance(llm.claude_llm(), llm.ClaudeCLI)
