"""Load env files from the app directory (used when .env is baked into the image)."""

from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent


def load_app_env() -> None:
    """Load .env.prod then .env; later files override earlier ones."""
    for name in (".env.prod", ".env"):
        path = _ROOT / name
        if path.is_file():
            load_dotenv(path, override=True)
