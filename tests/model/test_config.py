"""Settings: loaded with python-dotenv from a TEMP file (never the real .env); the key is never printable."""

import pytest

from errand.config import PRICE_TABLE, ConfigError, Settings

SENTINEL = "sk-ant-SENTINEL-do-not-print"


def test_price_table_has_the_given_sonnet_prices_and_they_are_estimates():
    assert PRICE_TABLE["claude-sonnet-5-5"] == (2.0, 10.0)  # USD per 1M input / output tokens
    s = Settings.load(env={})
    assert s.model_name == "claude-sonnet-5-5" and s.prices["claude-sonnet-5-5"] == (2.0, 10.0)
    over = Settings.load(env={"MODEL_NAME": "claude-sonnet-5-5", "PRICE_INPUT_PER_MTOK": "3", "PRICE_OUTPUT_PER_MTOK": "15"})
    assert over.prices["claude-sonnet-5-5"] == (3.0, 15.0) and PRICE_TABLE["claude-sonnet-5-5"] == (2.0, 10.0)


def test_defaults_and_overrides_of_model_settings():
    d = Settings.load(env={})
    assert (d.effort, d.thinking, d.max_tokens, d.timeout_s) == ("medium", "between_tools", 2048, 20.0)
    s = Settings.load(env={"MODEL_EFFORT": "none", "MODEL_THINKING": "none", "MODEL_MAX_TOKENS": "512", "MODEL_NAME": "x"})
    assert (s.effort, s.thinking, s.max_tokens, s.model_name) == (None, None, 512, "x")
    with pytest.raises(ConfigError):
        Settings.load(env={"MODEL_MAX_TOKENS": "lots"})


def test_the_key_comes_from_a_dotenv_file_but_is_never_stored_or_printable(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")  # registers cleanup for what dotenv sets below
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    env_file = tmp_path / "test.env"
    env_file.write_text(f"ANTHROPIC_API_KEY={SENTINEL}\nMODEL_NAME=claude-sonnet-5-5\n")
    s = Settings.load(env_file)
    assert s.api_key_set is True
    for text in (repr(s), str(s), f"{s}", repr(s.__dict__)):
        assert SENTINEL not in text
    assert "key=set" in repr(s)
    s.require_key()  # present: no error


def test_a_missing_dotenv_file_is_fine_and_a_missing_key_is_one_clear_line(tmp_path):
    s = Settings.load(tmp_path / "does-not-exist.env")
    assert s.api_key_set is False and "key=missing" in repr(s)
    with pytest.raises(ConfigError) as e:
        s.require_key()
    assert "ANTHROPIC_API_KEY" in str(e.value) and "\n" not in str(e.value)
