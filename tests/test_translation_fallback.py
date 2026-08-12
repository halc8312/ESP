"""
What happens to the translate button when the paid account stops answering.

Reported from the live site: pressing 翻訳する returned

    翻訳に失敗しました: OpenAI translation failed: Error code: 429 -
    {'error': {'message': 'You have no credits remaining. ...'}}

Nothing about that is the operator's to fix, and it hits every student at
once. So a failure of the account — rather than of the text — falls through
to the offline translator, and whatever the operator does end up reading is
in a language they speak.
"""
import pytest

from jobs.translation_tasks import _describe_failure
from services.translator.base import TranslationError, TranslatorUnavailableError
from services.translator.openai_backend import _is_account_unusable


class _FakeOpenAIError(Exception):
    """Shaped like the openai client's errors, which carry code and body."""

    def __init__(self, message, *, code=None, body=None):
        super().__init__(message)
        self.code = code
        self.body = body


class TestTellingTheTwoApart:
    def test_an_exhausted_balance_is_about_the_account(self):
        exc = _FakeOpenAIError(
            "Error code: 429 - {'error': {'message': 'You have no credits remaining.'}}",
            code="credit_balance_exhausted",
            body={"error": {"code": "credit_balance_exhausted", "type": "insufficient_quota"}},
        )
        assert _is_account_unusable(exc) is True

    @pytest.mark.parametrize(
        "code", ["insufficient_quota", "invalid_api_key", "account_deactivated"]
    )
    def test_the_other_account_conditions_count_too(self, code):
        assert _is_account_unusable(_FakeOpenAIError("boom", code=code)) is True

    @pytest.mark.parametrize(
        "message",
        [
            "Error code: 500 - internal server error",
            "Request timed out.",
            "Error code: 429 - rate limit reached, please slow down",
            "Connection error.",
        ],
    )
    def test_a_passing_problem_is_not(self, message):
        # These are worth retrying; they must not be mistaken for a dead
        # account, or a busy minute would push everyone onto the offline
        # translator for good.
        assert _is_account_unusable(_FakeOpenAIError(message)) is False


class TestWordingTheOperatorSees:
    def test_an_unusable_service_is_explained_in_japanese(self):
        message = _describe_failure(
            TranslatorUnavailableError("OpenAI account cannot serve translations: 429 ...")
        )
        assert message == "翻訳サービスに接続できませんでした。管理者にご連絡ください。"
        assert "429" not in message
        assert "OpenAI" not in message

    def test_an_unexpected_failure_keeps_its_detail(self):
        # Vague reassurance would be worse than the truth here.
        message = _describe_failure(TranslationError("segment 3 exceeded the token limit"))
        assert message == "segment 3 exceeded the token limit"


class TestWhenTheFallbackItselfCannotBeBuilt:
    def test_the_reason_is_written_down(self, monkeypatch, caplog):
        import logging

        import services.translator.registry as registry

        registry.reset_translator_backend_for_tests()
        monkeypatch.setenv("TRANSLATOR_BACKEND", "openai")

        def _broken(name):
            raise ImportError("argostranslate is not installed")

        monkeypatch.setattr(registry, "_instantiate_backend", _broken)

        with caplog.at_level(logging.ERROR, logger="services.translator.registry"):
            assert registry.get_fallback_translator_backend() is None

        # Without this, the only trace of the problem is a generic message on
        # the operator's screen.
        assert "argostranslate is not installed" in caplog.text
        registry.reset_translator_backend_for_tests()


class _DeadBackend:
    name = "dead"

    def translate_plain(self, text):
        raise TranslatorUnavailableError("no credits remaining")

    def translate_html(self, html):
        raise TranslatorUnavailableError("no credits remaining")


class _OfflineBackend:
    name = "argos"

    def translate_plain(self, text):
        return f"[offline] {text}"

    def translate_html(self, html):
        return f"<p>[offline] {html}</p>"


class TestFallingBackToTheOfflineTranslator:
    def test_the_job_still_produces_a_translation(self, monkeypatch, db_session):
        from models import Product, TranslationSuggestion, User
        from time_utils import utc_now
        import jobs.translation_tasks as tasks

        user = User(username="translate_fallback_user", last_login_at=utc_now())
        user.set_password("testpassword")
        db_session.add(user)
        db_session.commit()
        product = Product(
            user_id=user.id,
            site="manual",
            source_url="https://example.com/fallback",
            last_title="日本語の商品名",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db_session.add(product)
        db_session.commit()

        suggestion = TranslationSuggestion(
            job_id="fallback-job-1",
            product_id=product.id,
            user_id=user.id,
            scope="title",
            status="queued",
            source_title="日本語の商品名",
            source_description="",
        )
        db_session.add(suggestion)
        db_session.commit()

        monkeypatch.setattr(tasks, "get_translator_backend", _DeadBackend)
        monkeypatch.setattr(tasks, "get_fallback_translator_backend", _OfflineBackend)

        result = tasks.execute_translation_job("fallback-job-1")

        assert result["status"] == "succeeded"
        db_session.expire_all()
        stored = (
            db_session.query(TranslationSuggestion)
            .filter_by(job_id="fallback-job-1")
            .one()
        )
        assert stored.status == "succeeded"
        assert stored.translated_title == "[offline] 日本語の商品名"

    def test_without_an_offline_translator_the_failure_is_reported(
        self, monkeypatch, db_session
    ):
        from models import Product, TranslationSuggestion, User
        from time_utils import utc_now
        import jobs.translation_tasks as tasks

        user = User(username="translate_nofallback_user", last_login_at=utc_now())
        user.set_password("testpassword")
        db_session.add(user)
        db_session.commit()
        product = Product(
            user_id=user.id,
            site="manual",
            source_url="https://example.com/nofallback",
            last_title="日本語の商品名",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db_session.add(product)
        db_session.commit()
        db_session.add(
            TranslationSuggestion(
                job_id="fallback-job-2",
                product_id=product.id,
                user_id=user.id,
                scope="title",
                status="queued",
                source_title="日本語の商品名",
                source_description="",
            )
        )
        db_session.commit()

        monkeypatch.setattr(tasks, "get_translator_backend", _DeadBackend)
        monkeypatch.setattr(tasks, "get_fallback_translator_backend", lambda: None)

        with pytest.raises(TranslationError):
            tasks.execute_translation_job("fallback-job-2")

        db_session.expire_all()
        stored = (
            db_session.query(TranslationSuggestion)
            .filter_by(job_id="fallback-job-2")
            .one()
        )
        assert stored.status == "failed"
        assert stored.error_message == "翻訳サービスに接続できませんでした。管理者にご連絡ください。"
