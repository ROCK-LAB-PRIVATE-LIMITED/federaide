import json
import os
from pathlib import Path

FEDERAIDE_SYS_DIR = os.path.join(str(Path.home()), ".federaide")
DEFAULT_TEAM_DIR = os.path.join(str(Path.home()), ".federate")
GLOBAL_SETTINGS_FILE = os.path.join(FEDERAIDE_SYS_DIR, "global_settings.json")


def get_team_dir() -> str:
    """Lightweight team directory lookup without importing agent/LLM dependencies."""
    if os.path.exists(GLOBAL_SETTINGS_FILE):
        try:
            with open(GLOBAL_SETTINGS_FILE, "r", encoding="utf-8") as f:
                active = json.load(f).get("active_team_dir")
                if active:
                    return os.path.abspath(os.path.expanduser(active))
        except Exception:
            pass
    return DEFAULT_TEAM_DIR


def load_team_settings() -> dict:
    """Lightweight team settings lookup for standalone IDE mode."""
    defaults = {"theme": "tokyo-night"}
    path = os.path.join(get_team_dir(), "team_settings.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    return defaults