"""Unit tests for the OpenAI translator backend.

These tests exercise the backend without talking to the real OpenAI
API: they monkeypatch ``OpenAI`` with a stub client so we can assert
call arguments (model, messages, temperature) and simulate failures.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from services.translator import openai_backend as openai_backend_module
from services.translator.base import TranslationError, TranslatorUnavailableError
from services.translator.openai_backend import OpenAITranslatorBackend


class _StubChatCompletions:
    def __init__(self, response: Any = None, exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.response


class _StubOpenAIClient:
    """Minimal stand-in for ``openai.OpenAI`` used in tests."""

    last_init_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_init_kwargs = kwargs
        self._completions = _StubChatCompletions(
            response=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="Hello")
                    )
                ]
            )
        )
        self.chat = SimpleNamespace(completions=self._completions)

    # expose the completions stub for assertions
    @property
    def completions(self) -> _StubChatCompletions:
        return self._completions


def _install_stub(monkeypatch: pytest.MonkeyPatch, client_cls=_StubOpenAIClient) -> type:
    """Install a stub so ``from openai import OpenAI`` returns our class."""
    import sys
    import types

    module = types.ModuleType("openai")
    module.OpenAI = client_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", module)
    return client_cls


# ---------------------------------------------------------------------------
# construction / configuration
# ---------------------------------------------------------------------------


def test_uses_default_model_when_none_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_TRANSLATOR_MODEL", raising=False)
    backend = OpenAITranslatorBackend(api_key="sk-test")
    assert backend.model == "gpt-4.1-nano"


def test_reads_model_override_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TRANSLATOR_MODEL", "gpt-4o-mini")
    backend = OpenAITranslatorBackend(api_key="sk-test")
    assert backend.model == "gpt-4o-mini"


def test_raises_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _install_stub(monkeypatch)
    backend = OpenAITranslatorBackend(api_key=None)

    with pytest.raises(TranslatorUnavailableError):
        backend.translate_plain("こんにちは")


def test_raises_when_openai_package_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    # Force ``import openai`` to fail by blocking the module.
    monkeypatch.setitem(sys.modules, "openai", None)
    backend = OpenAITranslatorBackend(api_key="sk-test")

    with pytest.raises(TranslatorUnavailableError):
        backend.translate_plain("こんにちは")


# ---------------------------------------------------------------------------
# translate_plain
# ---------------------------------------------------------------------------


def test_translate_plain_returns_empty_for_blank_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stub(monkeypatch)
    backend = OpenAITranslatorBackend(api_key="sk-test")
    assert backend.translate_plain("") == ""
    assert backend.translate_plain("   ") == ""


def test_translate_plain_sends_expected_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stub(monkeypatch)
    backend = OpenAITranslatorBackend(api_key="sk-test")

    result = backend.translate_plain("こんにちは")

    assert result == "Hello"
    # Access the singleton stub via the backend's client
    stub: _StubOpenAIClient = backend._client  # type: ignore[assignment]
    assert len(stub.completions.calls) == 1
    call = stub.completions.calls[0]

    assert call["model"] == "gpt-4.1-nano"
    assert call["temperature"] == 0
    messages = call["messages"]
    assert messages[0]["role"] == "system"
    assert "Japanese" in messages[0]["content"]
    assert "English" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "こんにちは"}


def test_translate_plain_strips_whitespace_and_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoisyClient(_StubOpenAIClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._completions.response = SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="  Hello  \n")
                    )
                ]
            )

    _install_stub(monkeypatch, _NoisyClient)
    backend = OpenAITranslatorBackend(api_key="sk-test")
    assert backend.translate_plain("こんにちは") == "Hello"


def test_translate_plain_wraps_api_errors_in_translation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingClient(_StubOpenAIClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._completions.exc = RuntimeError("upstream timeout")

    _install_stub(monkeypatch, _FailingClient)
    backend = OpenAITranslatorBackend(api_key="sk-test")

    with pytest.raises(TranslationError) as excinfo:
        backend.translate_plain("こんにちは")

    assert "upstream timeout" in str(excinfo.value)


def test_translate_plain_raises_when_response_has_no_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _EmptyClient(_StubOpenAIClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._completions.response = SimpleNamespace(choices=[])

    _install_stub(monkeypatch, _EmptyClient)
    backend = OpenAITranslatorBackend(api_key="sk-test")

    with pytest.raises(TranslationError):
        backend.translate_plain("こんにちは")


# ---------------------------------------------------------------------------
# translate_html
# ---------------------------------------------------------------------------


def test_translate_html_preserves_tag_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Return a fixed mapping based on input content so we can assert
    # each text node got translated independently while markup stays.
    translations = {
        "こんにちは": "Hello",
        "世界": "World",
    }

    class _MappingClient(_StubOpenAIClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)

            def _create(**call_kwargs: Any) -> Any:
                self._completions.calls.append(call_kwargs)
                user_text = call_kwargs["messages"][-1]["content"]
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=translations.get(user_text, user_text)
                            )
                        )
                    ]
                )

            self._completions.create = _create  # type: ignore[assignment]

    _install_stub(monkeypatch, _MappingClient)
    backend = OpenAITranslatorBackend(api_key="sk-test")

    html = "<p>こんにちは<strong>世界</strong></p>"
    result = backend.translate_html(html)

    assert "Hello" in result
    assert "World" in result
    assert "<strong>World</strong>" in result


def test_translate_html_returns_empty_for_blank_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stub(monkeypatch)
    backend = OpenAITranslatorBackend(api_key="sk-test")
    assert backend.translate_html("") == ""
    assert backend.translate_html("   ") == ""


# ---------------------------------------------------------------------------
# client singleton behaviour
# ---------------------------------------------------------------------------


def test_client_is_constructed_once_and_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    construction_count = {"n": 0}

    class _CountingClient(_StubOpenAIClient):
        def __init__(self, **kwargs: Any) -> None:
            construction_count["n"] += 1
            super().__init__(**kwargs)

    _install_stub(monkeypatch, _CountingClient)
    backend = OpenAITranslatorBackend(api_key="sk-test")

    backend.translate_plain("こんにちは")
    backend.translate_plain("さようなら")
    backend.translate_plain("おはよう")

    assert construction_count["n"] == 1


def test_client_receives_configured_timeout_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stub(monkeypatch)
    backend = OpenAITranslatorBackend(
        api_key="sk-test",
        timeout_seconds=7.5,
        max_retries=5,
    )
    backend.translate_plain("こんにちは")

    init_kwargs = _StubOpenAIClient.last_init_kwargs
    assert init_kwargs is not None
    assert init_kwargs["api_key"] == "sk-test"
    assert init_kwargs["timeout"] == 7.5
    assert init_kwargs["max_retries"] == 5
