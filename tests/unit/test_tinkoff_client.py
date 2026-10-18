"""Тесты клиента Tinkoff: обработка повторов, маппинг интервалов, конвертации."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from src.errors import AuthenticationError
from src.tinkoff.client import (
    is_rate_limited,
    iter_candles,
    quotation_to_float,
    rate_limit_reset_secs,
    retry_call,
)


def _grpc_error(name: str, details: str = "") -> Exception:
    """Фейковая ошибка SDK с grpc-подобным атрибутом кода."""
    exc = RuntimeError(details or name)
    exc.code = SimpleNamespace(name=name)
    return exc


def _grpc_authenticated() -> Exception:
    return _grpc_error("UNAUTHENTICATED", "invalid token")


def make_stream(*candles):
    """Генераторный поток, повторяющий ленивый итератор свечей SDK."""

    def _stream(*args, **kwargs):
        yield from candles

    return _stream


def _timed(hours: int) -> SimpleNamespace:
    return SimpleNamespace(time=datetime(2024, 1, 1, hours, tzinfo=UTC))


class TestRateLimit:
    def test_is_rate_limited_matches_textual_resource_exhausted(self):
        assert is_rate_limited(RuntimeError("resource_exhausted error"))
        assert is_rate_limited(RuntimeError("RESOURCE_EXHAUSTED failed"))
        assert not is_rate_limited(RuntimeError("unavailable"))

    def test_is_rate_limited_matches_code(self):
        assert is_rate_limited(_grpc_error("RESOURCE_EXHAUSTED"))

    def test_rate_limit_reset_secs_parses_int(self):
        err = RuntimeError("RESOURCE_EXHAUSTED ratelimit_reset=12")
        assert rate_limit_reset_secs(err) == 12

    def test_rate_limit_reset_secs_returns_none_without_hint(self):
        assert rate_limit_reset_secs(RuntimeError("boom")) is None


class TestRetryCall:
    def test_success_on_first_try(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        assert retry_call(fn) == "ok"
        assert calls["n"] == 1

    def test_rate_limit_retried_then_succeeds(self):
        with patch("src.tinkoff.client.time.sleep"):
            calls = {"n": 0}

            def fn():
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("RESOURCE_EXHAUSTED ratelimit_reset=3")
                return "ok"

            assert retry_call(fn) == "ok"
            assert calls["n"] == 3

    def test_rate_limit_never_succeeds_raises_after_max_retries(self):
        with patch("src.tinkoff.client.time.sleep"):
            calls = {"n": 0}

            def fn():
                calls["n"] += 1
                raise RuntimeError("RESOURCE_EXHAUSTED")

            with pytest.raises(RuntimeError):
                retry_call(fn)
            assert calls["n"] == 4  # 1 initial + 3 retries

    def test_retry_delay_respects_ratelimit_reset_and_max_delay(self):
        import src.tinkoff.client as mod

        with patch.object(mod.time, "sleep") as sleep:
            calls = {"n": 0}

            def fn():
                calls["n"] += 1
                raise RuntimeError("RESOURCE_EXHAUSTED ratelimit_reset=5")

            with pytest.raises(RuntimeError):
                retry_call(fn, max_retries=3, base_delay=10, max_delay=60)
        # попытки: delay = min(10*2^a, 5, 60) → a=0:5, a=1:5, a=2:5
        assert sleep.call_count == 3
        assert all(call.args[0] <= 5 for call in sleep.call_args_list)

    def test_permanent_error_not_retried(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise _grpc_authenticated()

        with pytest.raises(RuntimeError):
            retry_call(fn)
        assert calls["n"] == 1

    def test_transient_unavailable_retried(self):
        with patch("src.tinkoff.client.time.sleep"):
            calls = {"n": 0}

            def fn():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise _grpc_error("UNAVAILABLE")
                return "recovered"

            assert retry_call(fn) == "recovered"
            assert calls["n"] == 2

    def test_non_retryable_error_not_retried(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            retry_call(fn)
        assert calls["n"] == 1


class TestIterCandles:
    def test_yields_stream_without_failures(self):
        candles = [_timed(0), _timed(1), _timed(2)]
        client = SimpleNamespace(get_all_candles=make_stream(*candles))
        results = list(
            iter_candles(client, "uid", datetime(2024, 1, 1), datetime(2024, 1, 2), "1h")
        )
        assert [c.time.hour for c in results] == [0, 1, 2]

    def test_reopens_stream_on_iteration_rate_limit(self):
        with patch("src.tinkoff.client.time.sleep"):
            opens = {"n": 0}

            def flaky_stream(instrument_id, from_, to, interval):
                opens["n"] += 1
                yield _timed(0)
                if opens["n"] == 1:
                    raise RuntimeError("RESOURCE_EXHAUSTED ratelimit_reset=2")
                yield _timed(1)
                yield _timed(2)

            client = SimpleNamespace(get_all_candles=flaky_stream)
            results = list(
                iter_candles(client, "uid", datetime(2024, 1, 1), datetime(2024, 1, 2), "1h")
            )
        assert opens["n"] == 2
        assert [c.time.hour for c in results] == [0, 1, 2]

    def test_interrupted_stream_rethrows_after_max_retries(self):
        with patch("src.tinkoff.client.time.sleep"):
            opens = {"n": 0}

            def broken_stream(instrument_id, from_, to, interval):
                opens["n"] += 1
                yield _timed(0)
                raise RuntimeError("RESOURCE_EXHAUSTED")

            client = SimpleNamespace(get_all_candles=broken_stream)
            with pytest.raises(RuntimeError):
                list(iter_candles(client, "uid", datetime(2024, 1, 1), datetime(2024, 1, 2), "1h"))
        assert opens["n"] == 4  # 1 initial + 3 retries


class TestConversions:
    def test_quotation_to_float(self):
        q = SimpleNamespace(units=123, nano=500_000_000)
        assert quotation_to_float(q) == pytest.approx(123.5)

    def test_quotation_to_float_none(self):
        assert quotation_to_float(None) == 0.0


class TestMapping:
    def test_authentication_error_wrapped(self):
        from src.tinkoff.client import wrap_client_error

        wrapped = wrap_client_error(_grpc_authenticated(), "operation")
        assert isinstance(wrapped, AuthenticationError)
