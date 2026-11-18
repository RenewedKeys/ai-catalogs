"""GEMINI_API_KEY as an alternative to an OpenRouter (OpenAI-compatible) key."""

from app.core.config import GEMINI_BASE_URL, Settings


def make(**env):
    return Settings(_env_file=None, **env)


def test_gemini_key_alone_switches_to_google():
    s = make(GEMINI_API_KEY="AIza-test")
    assert s.OPENAI_API_KEY == "AIza-test"
    assert s.OPENAI_BASE_URL == GEMINI_BASE_URL
    assert s.DEFAULT_MODEL == "gemini-3.5-flash-lite"


def test_openrouter_key_wins_when_both_are_set():
    s = make(GEMINI_API_KEY="AIza-test", OPENAI_API_KEY="sk-or-test")
    assert s.OPENAI_API_KEY == "sk-or-test" and "openrouter" in s.OPENAI_BASE_URL
    assert s.DEFAULT_MODEL == "google/gemini-3.5-flash-lite"


def test_explicit_base_url_and_model_are_kept():
    s = make(GEMINI_API_KEY="AIza-test", OPENAI_BASE_URL="https://proxy.example/v1", DEFAULT_MODEL="gemini-pro")
    assert s.OPENAI_BASE_URL == "https://proxy.example/v1" and s.DEFAULT_MODEL == "gemini-pro"


def test_no_keys_change_nothing():
    s = make()
    assert s.OPENAI_API_KEY is None and "openrouter" in s.OPENAI_BASE_URL
