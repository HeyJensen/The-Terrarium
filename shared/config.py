"""Settings and secrets.

Non-secret settings live in config/settings.json (committed).
Secrets live ONLY in environment variables, optionally loaded from the
git-ignored config/.env. Nothing here ever prints a secret value.
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.json"
DOTENV_PATH = ROOT / "config" / ".env"

# Env var name suffixes treated as secret. The logger redacts their values.
SECRET_SUFFIXES = ("_TOKEN", "_SECRET", "_KEY", "_PASSWORD", "_TOKEN_FILE")


def load_dotenv(path: Path = DOTENV_PATH) -> None:
    """Load KEY=VALUE lines into os.environ without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    load_dotenv()
    with open(path) as f:
        return json.load(f)


def env(name: str, required: bool = False) -> str | None:
    value = os.environ.get(name) or None
    if required and value is None:
        # Name only, never a value.
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def is_secret_name(name: str) -> bool:
    return name.upper().endswith(SECRET_SUFFIXES)


def secret_values() -> list[str]:
    """Current values of every secret-looking env var, for redaction."""
    return [v for k, v in os.environ.items() if is_secret_name(k) and v and len(v) >= 4]
