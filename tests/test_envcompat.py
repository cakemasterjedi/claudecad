"""fsgen -> ScriptForge rename: env var fallback, legacy state-file migration, .env password rewrite."""
from pathlib import Path

from scriptforge.envcompat import env, migrate_state
from scriptforge.web.password_api import save_password


def test_env_prefers_new_name_and_falls_back(monkeypatch):
    monkeypatch.delenv("SCRIPTFORGE_X_TEST", raising=False)
    monkeypatch.delenv("FSGEN_X_TEST", raising=False)
    assert env("X_TEST") is None and env("X_TEST", "d") == "d"
    monkeypatch.setenv("FSGEN_X_TEST", "old")
    assert env("X_TEST", "d") == "old"
    monkeypatch.setenv("SCRIPTFORGE_X_TEST", "new")
    assert env("X_TEST") == "new"


def test_migrate_state_moves_legacy_file(tmp_path):
    old, new = tmp_path / ".fsgen_api_ledger.jsonl", tmp_path / ".scriptforge_api_ledger.jsonl"
    old.write_text('{"call": 1}\n')
    assert migrate_state(new).read_text() == '{"call": 1}\n'
    assert not old.exists()


def test_migrate_state_keeps_existing_new_file_and_tolerates_absence(tmp_path):
    new = tmp_path / ".scriptforge_native.json"
    assert not migrate_state(new).exists()  # both absent: no error
    old = tmp_path / ".fsgen_native.json"
    new.write_text("new")
    old.write_text("old")
    migrate_state(new)
    assert new.read_text() == "new" and old.read_text() == "old"  # never overwrite
    assert migrate_state(Path(tmp_path / "other.json")).name == "other.json"


def test_ledger_read_through_usage_migrates(tmp_path, monkeypatch):
    import scriptforge.usage as usage
    ledger = tmp_path / ".scriptforge_api_ledger.jsonl"
    (tmp_path / ".fsgen_api_ledger.jsonl").write_text('{"t": "2026-01-01T00:00:00", "run": "r", "purpose": "p", '
                                                      '"call": "GET x", "status": 200, "counted": true}\n')
    monkeypatch.setattr(usage, "LEDGER", ledger)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONSHAPE_API_BASELINE", "0")
    monkeypatch.setenv("ONSHAPE_API_BASELINE_DATE", "")
    alloc = usage.allocation_status()
    assert alloc.used == 1  # the legacy ledger row was read through the new path
    assert ledger.exists() and not (tmp_path / ".fsgen_api_ledger.jsonl").exists()


def test_save_password_replaces_legacy_line(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\nFSGEN_WEB_PASSWORD='oldpassword'\nB=2\n")
    save_password(env_file, "newpassword1")
    text = env_file.read_text()
    assert "FSGEN_WEB_PASSWORD" not in text
    assert text.count("SCRIPTFORGE_WEB_PASSWORD='newpassword1'") == 1 and "A=1" in text and "B=2" in text
