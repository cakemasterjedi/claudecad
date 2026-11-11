"""ScanRuler updater with a mocked subprocess runner (no network, git or npm)."""
import fcntl
import os

import pytest

from scriptforge import scanruler as sr

LS = "\n".join(f"abc\trefs/tags/{t}" for t in ["v0.9.9", "v0.11.1", "v0.12.0", "v1.0.0-rc1", "nightly"])


def make_build(d, ok=True):
    (d / "dist-sf/assets").mkdir(parents=True)
    (d / "dist-sf/assets/a.js").write_text("x")
    (d / "dist-sf/index.html").write_text('<script src="/scanruler/assets/a.js"></script>' if ok else
                                          '<script src="/scanruler/assets/gone.js"></script>')


class Runner:
    def __init__(self, ls=LS, node="v22.1.0", fail=None, build_ok=True):
        self.calls, self.ls, self.node, self.fail, self.build_ok = [], ls, node, fail, build_ok

    def __call__(self, cmd, cwd, timeout, log):
        self.calls.append(cmd)
        if cmd[0] == "git" and cmd[1] == "ls-remote":
            return 0, self.ls
        if cmd[0] == "node":
            return 0, self.node
        if self.fail and cmd[:2] == self.fail:
            return 1, ""
        if cmd[:2] == ["npx", "vite"]:
            make_build(cwd, self.build_ok)
        return 0, ""


def link(base, tag):
    (base / "current").symlink_to(f"{tag}/dist-sf")


def test_pick_latest():
    assert sr.pick_latest(LS) == "v0.12.0"
    assert sr.pick_latest("a\trefs/tags/v1.0.0-rc1") is None
    assert sr.pick_latest("a\trefs/tags/v0.9.9\nb\trefs/tags/v0.10.0") == "v0.10.0"


def test_adopt_existing(tmp_path):
    ls = "a\trefs/tags/v0.11.1"
    make_build(tmp_path / "v0.11.1")
    r = Runner(ls)
    assert sr.update(tmp_path, r).startswith("adopted")
    assert sr.installed_version(tmp_path) == "0.11.1"
    assert all(c[0] != "npm" for c in r.calls)
    assert sr.update(tmp_path, r) == "up-to-date"
    st = sr.read_state(tmp_path)
    assert st["latest"] == "0.11.1" and st["error"] is None


def test_success_swaps_and_prunes(tmp_path):
    for t in ("v0.9.0", "v0.10.0", "v0.11.1"):
        make_build(tmp_path / t)
    link(tmp_path, "v0.9.0")  # current is old; it must survive pruning
    r = Runner()
    assert sr.update(tmp_path, r) == "installed v0.12.0"
    assert sr.installed_version(tmp_path) == "0.12.0"
    assert not (tmp_path / ".staging-v0.12.0").exists()
    left = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("v"))
    assert left == ["v0.11.1", "v0.12.0"]
    assert ["npx", "vite", "build", "--base=/scanruler/", "--outDir", "dist-sf", "--emptyOutDir"] in r.calls


def test_prune_never_deletes_current(tmp_path):
    for t in ("v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0"):
        make_build(tmp_path / t)
    link(tmp_path, "v0.1.0")
    sr.prune(tmp_path)
    assert (tmp_path / "v0.1.0").exists() and not (tmp_path / "v0.2.0").exists()


@pytest.mark.parametrize("kw", [{"fail": ["npm", "ci"]}, {"build_ok": False}, {"node": "v20.0.0"},
                                {"fail": ["git", "clone"]}])
def test_failure_keeps_current(tmp_path, kw):
    make_build(tmp_path / "v0.11.1")
    link(tmp_path, "v0.11.1")
    with pytest.raises(sr.UpdateError):
        sr.update(tmp_path, Runner(**kw))
    assert sr.installed_version(tmp_path) == "0.11.1"
    assert not (tmp_path / ".staging-v0.12.0").exists() and not (tmp_path / "v0.12.0").exists()
    assert sr.read_state(tmp_path)["error"]
    assert sr.info(tmp_path)["error"]


def test_lock_blocks_concurrent(tmp_path):
    f = open(tmp_path / "update.lock", "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert sr.is_running(tmp_path)
    with pytest.raises(sr.AlreadyRunning):
        sr.update(tmp_path, Runner())
    f.close()
    assert not sr.is_running(tmp_path)


def test_status_text(tmp_path):
    assert "installed: no" in sr.status_text(tmp_path)
