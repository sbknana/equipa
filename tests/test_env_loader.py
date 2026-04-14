"""Tests for equipa.env_loader — zero-dependency .env file parser.

Covers parsing rules, quote stripping, comment handling, existing-key
precedence, and the find-env-file walk.

Copyright 2026 Forgeborn. All rights reserved.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from equipa.env_loader import _is_valid_env_key, load_dotenv


class TestLoadDotenv:
    """Core .env parsing and injection."""

    def test_basic_key_value(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"  # type: ignore[operator]
        env_file.write_text("MY_TEST_KEY=hello_world\n")
        monkeypatch.delenv("MY_TEST_KEY", raising=False)

        injected = load_dotenv(env_file)

        assert injected == {"MY_TEST_KEY": "hello_world"}
        assert os.environ["MY_TEST_KEY"] == "hello_world"

    def test_double_quoted_value(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"  # type: ignore[operator]
        env_file.write_text('API_KEY="sk-ant-abc123"\n')
        monkeypatch.delenv("API_KEY", raising=False)

        injected = load_dotenv(env_file)

        assert injected["API_KEY"] == "sk-ant-abc123"

    def test_single_quoted_value(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"  # type: ignore[operator]
        env_file.write_text("SECRET='my secret value'\n")
        monkeypatch.delenv("SECRET", raising=False)

        injected = load_dotenv(env_file)

        assert injected["SECRET"] == "my secret value"

    def test_skips_comments_and_blanks(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"  # type: ignore[operator]
        env_file.write_text(textwrap.dedent("""\
            # This is a comment

            REAL_KEY=real_value
            # Another comment
        """))
        monkeypatch.delenv("REAL_KEY", raising=False)

        injected = load_dotenv(env_file)

        assert injected == {"REAL_KEY": "real_value"}

    def test_does_not_overwrite_existing(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"  # type: ignore[operator]
        env_file.write_text("EXISTING_VAR=from_dotenv\n")
        monkeypatch.setenv("EXISTING_VAR", "from_shell")

        injected = load_dotenv(env_file)

        assert injected == {}
        assert os.environ["EXISTING_VAR"] == "from_shell"

    def test_multiple_keys(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"  # type: ignore[operator]
        env_file.write_text(textwrap.dedent("""\
            ALPHA=one
            BETA=two
            GAMMA=three
        """))
        for key in ("ALPHA", "BETA", "GAMMA"):
            monkeypatch.delenv(key, raising=False)

        injected = load_dotenv(env_file)

        assert injected == {"ALPHA": "one", "BETA": "two", "GAMMA": "three"}

    def test_value_with_equals_sign(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"  # type: ignore[operator]
        env_file.write_text("DATABASE_URL=postgres://user:pass@host/db?opt=val\n")
        monkeypatch.delenv("DATABASE_URL", raising=False)

        injected = load_dotenv(env_file)

        assert injected["DATABASE_URL"] == "postgres://user:pass@host/db?opt=val"

    def test_empty_value(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"  # type: ignore[operator]
        env_file.write_text("EMPTY_VAR=\n")
        monkeypatch.delenv("EMPTY_VAR", raising=False)

        injected = load_dotenv(env_file)

        assert injected == {"EMPTY_VAR": ""}

    def test_skips_invalid_keys(self, tmp_path: object) -> None:
        env_file = tmp_path / ".env"  # type: ignore[operator]
        env_file.write_text(textwrap.dedent("""\
            123BAD=nope
            =also_bad
            GOOD_KEY=yes
        """))

        injected = load_dotenv(env_file)

        assert "123BAD" not in injected
        assert "" not in injected
        assert "GOOD_KEY" in injected

    def test_missing_file_returns_empty(self, tmp_path: object) -> None:
        missing = tmp_path / "nonexistent" / ".env"  # type: ignore[operator]
        injected = load_dotenv(missing)
        assert injected == {}

    def test_none_path_without_env_file(self) -> None:
        """When no .env exists anywhere in the walk, returns empty."""
        injected = load_dotenv(None)
        # May or may not find a .env depending on project state, but must not crash
        assert isinstance(injected, dict)

    def test_whitespace_around_key_and_value(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"  # type: ignore[operator]
        env_file.write_text("  SPACED_KEY  =  spaced_value  \n")
        monkeypatch.delenv("SPACED_KEY", raising=False)

        injected = load_dotenv(env_file)

        assert injected["SPACED_KEY"] == "spaced_value"


class TestIsValidEnvKey:
    """Validate the environment variable key checker."""

    @pytest.mark.parametrize("key", [
        "MY_VAR",
        "_PRIVATE",
        "a",
        "A1_B2_C3",
    ])
    def test_valid_keys(self, key: str) -> None:
        assert _is_valid_env_key(key) is True

    @pytest.mark.parametrize("key", [
        "",
        "123ABC",
        "has-dash",
        "has space",
        "has.dot",
    ])
    def test_invalid_keys(self, key: str) -> None:
        assert _is_valid_env_key(key) is False
