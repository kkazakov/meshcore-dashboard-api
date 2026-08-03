"""Tests for app.config.Settings — pydantic-settings configuration.

Regression: every variable present in .env.example must be declared on
Settings, otherwise pydantic-settings (extra=forbid) crashes at startup
with "Extra inputs are not permitted".
"""

from app.config import Settings


def test_settings_path_hash_mode_default(monkeypatch):
    """PATH_HASH_MODE defaults to 1 (2-byte hashes)."""
    monkeypatch.delenv("PATH_HASH_MODE", raising=False)
    assert Settings().path_hash_mode == 1


def test_settings_path_hash_mode_from_env(monkeypatch):
    """PATH_HASH_MODE in the environment is accepted and coerced to int."""
    monkeypatch.setenv("PATH_HASH_MODE", "0")
    assert Settings().path_hash_mode == 0


def test_settings_accepts_env_example_file(tmp_path, monkeypatch):
    """A .env containing every .env.example key must not raise."""
    example_lines = []
    with open(".env.example", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                example_lines.append(line)

    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(example_lines) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    Settings()  # must not raise ValidationError
