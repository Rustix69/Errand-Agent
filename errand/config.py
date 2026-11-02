"""Configuration. Loaded with python-dotenv only; the API key is NEVER stored in a printable object, never
logged and never traced: ``Settings`` holds only whether a key is present.

Env vars (names only; values live in the untracked ``.env`` or the environment):
  ANTHROPIC_API_KEY   read by the SDK from the environment (never by our code)
  MODEL_NAME          default claude-sonnet-5-5
  MODEL_EFFORT        low|medium|high|none      (default medium; "none" = do not send effort)
  MODEL_THINKING      between_tools|none        (default between_tools; "none" = do not send `thinking`)
  MODEL_MAX_TOKENS    default 2048
  MODEL_TIMEOUT_S     per API request, default 20
  PRICE_INPUT_PER_MTOK / PRICE_OUTPUT_PER_MTOK   override the active model's prices (USD per 1M tokens)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MODEL = "claude-sonnet-5-5"

# USD per 1M tokens (input, output). These are ESTIMATES used to put a cost next to real token usage: they are
# not read from the API response. Cache read/write tokens are recorded in the trace but not priced.
# "scripted" is a stand-in so offline traces look like real ones (its token counts are synthetic too).
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "scripted": (3.0, 15.0),
}


class ConfigError(Exception):
    """A setting is missing or invalid. Messages never contain secret values."""


def _opt(value: str | None, default: str | None) -> str | None:
    if value is None or value == "":
        return default
    return None if value.lower() == "none" else value


@dataclass(frozen=True)
class Settings:
    model_name: str = DEFAULT_MODEL
    api_key_set: bool = False
    effort: str | None = "medium"
    thinking: str | None = "between_tools"
    max_tokens: int = 2048
    timeout_s: float = 20.0
    prices: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(PRICE_TABLE))

    def __repr__(self) -> str:  # explicit, so a secret can never sneak into a log line
        return (f"Settings(model={self.model_name!r}, key={'set' if self.api_key_set else 'missing'}, "
                f"effort={self.effort!r}, thinking={self.thinking!r})")

    __str__ = __repr__

    @staticmethod
    def load(dotenv_path: str | Path | None = None, env: dict[str, str] | None = None) -> "Settings":
        """`dotenv_path` is loaded with python-dotenv (a missing file is fine). Tests pass a temp path so the
        real .env is never touched; the CLI passes the project's own."""
        if dotenv_path is not None:
            load_dotenv(dotenv_path, override=False)
        e = os.environ if env is None else env
        model = e.get("MODEL_NAME") or DEFAULT_MODEL
        prices = dict(PRICE_TABLE)
        try:
            if e.get("PRICE_INPUT_PER_MTOK") and e.get("PRICE_OUTPUT_PER_MTOK"):
                prices[model] = (float(e["PRICE_INPUT_PER_MTOK"]), float(e["PRICE_OUTPUT_PER_MTOK"]))
            return Settings(
                model_name=model, api_key_set=bool(e.get("ANTHROPIC_API_KEY")),
                effort=_opt(e.get("MODEL_EFFORT"), "medium"), thinking=_opt(e.get("MODEL_THINKING"), "between_tools"),
                max_tokens=int(e.get("MODEL_MAX_TOKENS") or 2048), timeout_s=float(e.get("MODEL_TIMEOUT_S") or 20),
                prices=prices,
            )
        except ValueError as ex:
            raise ConfigError(f"a numeric setting is invalid ({ex})") from None

    def require_key(self) -> None:
        if not self.api_key_set:
            raise ConfigError("ANTHROPIC_API_KEY is not set. Put it in .env (kept out of git) or export it, then retry.")
