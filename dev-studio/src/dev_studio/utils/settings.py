"""Persistent settings for Dev Studio (LM Studio URL, API key, model name).

Priority: settings file (~/.dev_studio_settings.json) → env vars → built-in defaults.
Defaults are evaluated lazily on every call so env var changes take effect without restart.
"""
import json
import os

_SETTINGS_FILE = os.path.expanduser("~/.dev_studio_settings.json")
_KEYS = ("lm_base_url", "lm_api_key", "model_name", "tavily_api_key")


def _env_defaults() -> dict:
    return {
        "lm_base_url":    os.environ.get("LM_BASE_URL",    "http://localhost:1234/v1"),
        "lm_api_key":     os.environ.get("LM_API_KEY",     "lm-studio"),
        "model_name":     os.environ.get("LM_MODEL",       ""),
        "tavily_api_key": os.environ.get("TAVILY_API_KEY", ""),
    }


def load_settings() -> dict:
    base = _env_defaults()
    if os.path.exists(_SETTINGS_FILE):
        try:
            with open(_SETTINGS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            # File values win over env vars, but only if non-empty
            base.update({k: v for k, v in saved.items() if k in _KEYS and v})
        except Exception:
            pass
    return base


def save_settings(data: dict) -> None:
    current = load_settings()
    current.update({k: v for k, v in data.items() if k in _KEYS})
    with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)
