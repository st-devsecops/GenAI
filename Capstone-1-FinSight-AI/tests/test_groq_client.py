import httpx

from finsight.llm.providers import GroqClient, LLMError, _groq_retry_delay


def test_groq_client_uses_bearer_auth():
    client = GroqClient("test-model", "https://example.test", "test-key")
    try:
        assert client._http.headers["Authorization"] == "Bearer test-key"
    finally:
        client._http.close()


def test_groq_rate_limit_delay_uses_retry_after():
    response = httpx.Response(
        429,
        headers={"Retry-After": "3"},
        request=httpx.Request("POST", "https://example.test"),
    )
    assert _groq_retry_delay(response) == 3


def test_groq_rate_limit_delay_parses_reset_duration():
    response = httpx.Response(
        429,
        headers={"x-ratelimit-reset-tokens": "1500ms"},
        request=httpx.Request("POST", "https://example.test"),
    )
    assert _groq_retry_delay(response) == 1.5


def test_groq_client_retries_rate_limit(monkeypatch):
    client = GroqClient("test-model", "https://example.test", "test-key")
    calls = []

    class MockClient:
        def post(self, *_args, **_kwargs):
            calls.append(None)
            if len(calls) == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "0"},
                    request=httpx.Request("POST", "https://example.test"),
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
                request=httpx.Request("POST", "https://example.test"),
            )

    client._http = MockClient()
    monkeypatch.setattr("finsight.llm.providers.time.sleep", lambda _delay: None)
    result = client.chat([{"role": "user", "content": "hello"}])
    assert result.text == "ok"
    assert len(calls) == 2


def test_groq_client_reports_long_rate_limit():
    client = GroqClient("test-model", "https://example.test", "test-key")

    class MockClient:
        def post(self, *_args, **_kwargs):
            return httpx.Response(
                429,
                headers={"Retry-After": "61"},
                request=httpx.Request("POST", "https://example.test"),
            )

    client._http = MockClient()
    try:
        client.chat([{"role": "user", "content": "hello"}])
    except LLMError as exc:
        assert "retry after 61s" in str(exc)
    else:
        raise AssertionError("Expected a rate-limit error")
