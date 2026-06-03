from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_DIR.parent.parent


def load_environment() -> None:
    """Load repo root .env first, then local overrides."""
    load_dotenv(REPO_ROOT / ".env")
    load_dotenv(PROJECT_DIR / ".env", override=True)
