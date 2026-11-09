"""Clarifying questions and variants: prompts, parsing, the pipeline early return and the CLI."""
import json
from pathlib import Path

import pytest

from scriptforge import cli
from scriptforge.native import generate as gen
from scriptforge.native.generate import extract_questions, split_questions
from scriptforge.native.prompts import append_answers, generate_prompt, variant_note
from tests.test_generate_loop import PLATE, FakeLLM, block

QUESTIONS = ("Questions:\n1. Which style? Options: round | square\n2. Which size range?\n")


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "a")
    monkeypatch.setenv("ONSHAPE_SECRET_KEY", "b")
    monkeypatch.setattr(gen, "render", lambda *a, **k: None)

    def _run(replies, **kw):
        fake = FakeLLM(replies)
        monkeypatch.setattr(gen, "make_llm", lambda *a, **k: fake)
        res = gen.generate_native("a plate", tmp_path / "out", name="Plate", paste=True, **kw)
        return res, fake
    return _run


def test_questions_only_reply_with_ask(run, tmp_path):
    res, fake = run([QUESTIONS], ask=True)
    assert res["ok"] is False and res["error"] is None and res["api_calls"] == 0
    assert res["questions"] == [{"text": "Which style?", "options": ["round", "square"], "compare": False},
                                {"text": "Which size range?", "options": [], "compare": False}]
    assert len(fake.prompts) == 1 and "`Questions:`" in fake.prompts[0]
    assert json.loads((tmp_path / "out/questions.json").read_text()) == res["questions"]
    assert json.loads((tmp_path / "out/result.json").read_text())["questions"] == res["questions"]
    assert not (tmp_path / "out/part_studio.fs").exists()


def test_questions_only_reply_without_ask_is_ignored(run):
    res, fake = run([QUESTIONS])
    assert res["ok"] is False and res["questions"] == []
    assert "`Questions:`" not in fake.prompts[0]


def test_questions_and_script_builds_normally(run, tmp_path):
    res, _ = run([QUESTIONS + "\nAssumptions:\n- x\n\n" + block(PLATE), "LGTM"], ask=True)
    assert res["ok"] and res["questions"] == []
    assert not (tmp_path / "out/questions.json").exists()


def test_extract_questions_formats():
    text = "**Questions:**\n1) Pick one **Options:** a | b | c\n2) Plain\n3. Third\n4. Fourth\n5. Fifth\n"
    qs = extract_questions(text)
    assert len(qs) == 4
    assert qs[0] == {"text": "Pick one", "options": ["a", "b", "c"], "compare": False}
    assert qs[1] == {"text": "Plain", "options": [], "compare": False}
    assert extract_questions("no section here") == []


def test_extract_questions_compare_marker():
    qs = extract_questions("Questions:\n1. Which top? Options: flat | domed [compare]\n2. Which? [compare]\n")
    assert qs[0] == {"text": "Which top?", "options": ["flat", "domed"], "compare": True}
    assert qs[1] == {"text": "Which?", "options": [], "compare": False}  # nothing to compare without 2 options


def test_append_answers_skips_blank():
    out = append_answers("a plate", [{"question": "Style?", "answer": "round"}, {"question": "Size?", "answer": " "}])
    assert out.startswith("a plate") and "Answers to your clarifying questions:" in out
    assert "- Q: Style?" in out and "A: round" in out and "Size?" not in out
    assert append_answers("a plate", [{"question": "Q", "answer": ""}]) == "a plate"


def test_prompt_flags():
    assert "`Questions:`" not in generate_prompt("x")
    assert "`Questions:`" in generate_prompt("x", ask=True)
    assert "Make this version: tall and thin" in generate_prompt("x", variant_note="tall and thin")


def test_variant_note():
    assert "most likely interpretation" in variant_note("A", "")
    b = variant_note("B", None, "- 5 mm walls")
    assert "different alternative" in b and "- 5 mm walls" in b
    assert variant_note("B", "  wide  ") == "wide"


class Recorder:
    name = "fake"

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, system, prompt, image=None, images=()):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else "LGTM"


@pytest.fixture
def cli_run(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "a")
    monkeypatch.setenv("ONSHAPE_SECRET_KEY", "b")
    monkeypatch.setattr(gen, "render", lambda *a, **k: None)

    def _go(replies, argv):
        fake = Recorder(replies)
        monkeypatch.setattr(gen, "make_llm", lambda *a, **k: fake)
        with pytest.raises(SystemExit) as e:
            cli.main(["generate", "a plate", "--paste", "--no-review", *argv])
        code = e.value.code
        return code, fake, capsys.readouterr().out
    return _go


def _json(out):
    return json.loads(out[out.index("{"):])


def test_cli_variants(cli_run, tmp_path):
    ok = "Assumptions:\n- wall 3 mm\n\n" + block(PLATE)
    code, fake, out = cli_run([ok, ok], ["--variant", "tall", "--variant", "", "--out", str(tmp_path / "o")])
    assert code == 0
    assert [v["label"] for v in _json(out)["variants"]] == ["A", "B"]
    assert "Make this version: tall" in fake.prompts[0]
    assert "different alternative to version A" in fake.prompts[1] and "wall 3 mm" in fake.prompts[1]
    assert (tmp_path / "o/A/part_studio.fs").exists() and (tmp_path / "o/B/part_studio.fs").exists()


def test_cli_ab(cli_run):
    ok = block(PLATE)
    code, fake, out = cli_run([ok, ok], ["--ab"])
    assert code == 0 and len(fake.prompts) == 2
    assert "most likely interpretation" in fake.prompts[0]


def test_cli_ask_without_tty_exits_2(cli_run, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    code, fake, out = cli_run([QUESTIONS], ["--ask"])
    assert code == 2 and len(fake.prompts) == 1
    assert _json(out)["questions"][0]["options"] == ["round", "square"]


def test_cli_ask_with_tty_reruns_with_answers(cli_run, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda *a: "2")
    code, fake, out = cli_run([QUESTIONS, block(PLATE)], ["--ask", "--out", str(tmp_path / "o")])
    assert code == 0 and len(fake.prompts) == 2
    assert "A: square" in fake.prompts[1] and "`Questions:`" not in fake.prompts[1]
    assert (tmp_path / "o-answered/part_studio.fs").exists()


def test_split_questions():
    text = "- 80 mm wide\n- 5 mm hole\n\n**Questions:**\n1. Which version? Options: A | B\n2. Which hole?\n"
    body, qs = split_questions(text)
    assert body == "- 80 mm wide\n- 5 mm hole" and qs[0] == {"text": "Which version?", "options": ["A", "B"], "compare": False}
    assert qs[1]["options"] == []
    assert split_questions("- plain list") == ("- plain list", [])
    assert "`Questions:`" in __import__("scriptforge.native.prompts", fromlist=["x"]).measure_prompt()[1]
