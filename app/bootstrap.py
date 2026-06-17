import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_DIR.parent.parent


def load_environment() -> None:
    """Load env files outer → inner; later files override earlier ones."""
    load_dotenv(REPO_ROOT / ".env")
    load_dotenv(PROJECT_DIR.parent / ".env")  # whatsapp_assistant/
    load_dotenv(PROJECT_DIR / ".env", override=True)
    _configure_tracing()


def _configure_tracing() -> None:
    """Ensure LangSmith environment variables are set for auto-instrumentation.

    LangSmith supports two naming conventions for its env vars:
      - Modern:  LANGSMITH_TRACING, LANGSMITH_API_KEY, LANGSMITH_PROJECT
      - Legacy:  LANGCHAIN_TRACING_V2, LANGCHAIN_API_KEY, LANGCHAIN_PROJECT

    The client accepts both, but we normalise here so whichever convention is
    used in .env, both sets of vars are present for any LangChain version.
    """
    pairs = [
        ("LANGSMITH_TRACING",   "LANGCHAIN_TRACING_V2"),
        ("LANGSMITH_API_KEY",   "LANGCHAIN_API_KEY"),
        ("LANGSMITH_PROJECT",   "LANGCHAIN_PROJECT"),
        ("LANGSMITH_ENDPOINT",  "LANGCHAIN_ENDPOINT"),
    ]
    for modern, legacy in pairs:
        # Prefer the modern key; fall back to legacy; ignore if neither is set.
        value = os.getenv(modern) or os.getenv(legacy)
        if value:
            os.environ[modern] = value
            os.environ[legacy] = value
