"""OpenRouter primary tier: transport, priority, fallback, and key safety."""

import io
import json
import os
from contextlib import contextmanager
from urllib.error import HTTPError, URLError

from noema.domain.activity import ActivityEvent
from noema.runtime import DaemonConfig
from noema.cli.daemon import _classifier, _openrouter_providers
from noema.application.classification import Classifier
from noema.infrastructure.providers import GeminiProvider, ProviderChain, OpenRouterProvider, ProviderError
from noema.domain.sessions import Sessionizer


TEST_KEY_ENV = "NOEMA_TEST_OPENROUTER_KEY"
TEST_KEY = "test-only-openrouter-secret"


@contextmanager
def test_key(value=TEST_KEY):
    previous = os.environ.get(TEST_KEY_ENV)
    os.environ[TEST_KEY_ENV] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(TEST_KEY_ENV, None)
        else:
            os.environ[TEST_KEY_ENV] = previous


def session():
    event = ActivityEvent(
        timestamp="2026-09-04T10:00:00Z",
        duration=120,
        device="laptop",
        app="VS Code",
        title="reward.py",
        bucket_id="window_laptop",
        source_event_id="1",
    )
    return Sessionizer().sessionize([event])[0]


def valid_text(category="productive"):
    return json.dumps({
        "category": category,
        "subcategory": "research",
        "activity": "Classified work",
        "signal": "The observed evidence supports this result.",
        "topic": "work",
        "project": None,
        "service": "test",
        "intent_signal": "working",
        "activity_type": "coding",
        "productivity": "productive",
        "confidence": 0.9,
    })


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.payload


def ok_transport(calls, text=None):
    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return FakeResponse(json.dumps({
            "choices": [{"message": {"content": text or valid_text()}}],
        }).encode("utf-8"))
    return fake_urlopen


def error_transport(code):
    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, code, "error", {}, io.BytesIO(b"{}"))
    return fake_urlopen


def test_openrouter_classify_sends_bearer_key_and_parses_json():
    calls = []
    with test_key():
        provider = OpenRouterProvider(
            model="minimax/minimax-m3:free", api_key_env=TEST_KEY_ENV,
            urlopen_fn=ok_transport(calls),
        )
        payload = provider.classify(session(), "classify this")

    assert payload["category"] == "productive"
    request, timeout = calls[0]
    assert request.full_url == "https://openrouter.ai/api/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer {}".format(TEST_KEY)
    assert timeout == 60.0
    body = json.loads(request.data.decode("utf-8"))
    assert body["model"] == "minimax/minimax-m3:free"
    assert body["response_format"] == {"type": "json_object"}
    # The secret travels only in the Authorization header.
    assert TEST_KEY not in request.full_url
    assert TEST_KEY not in request.data.decode("utf-8")


def test_openrouter_batch_returns_one_payload_per_session():
    calls = []

    def batch_transport(request, timeout):
        calls.append((request, timeout))
        body = json.loads(request.data.decode("utf-8"))
        assert body["model"] == "minimax/minimax-m3:free"
        assert body["max_tokens"] == 1600
        array_text = json.dumps([
            dict(json.loads(valid_text("productive")), session_id="a"),
            dict(json.loads(valid_text("distractive")), session_id="b"),
        ])
        return FakeResponse(json.dumps({
            "choices": [{"message": {"content": array_text}}],
        }).encode("utf-8"))

    with test_key():
        provider = OpenRouterProvider(
            model="minimax/minimax-m3:free", api_key_env=TEST_KEY_ENV,
            urlopen_fn=batch_transport,
        )
        items = provider.classify_batch(
            ["a", "b"],
            [{"evidence": "one"}, {"evidence": "two"}],
        )
    assert [item["session_id"] for item in items] == ["a", "b"]
    assert calls[0][0].full_url.endswith("/chat/completions")


def test_openrouter_batch_rejects_mismatched_counts():
    with test_key():
        provider = OpenRouterProvider(model="x", api_key_env=TEST_KEY_ENV)
        try:
            provider.classify_batch(["a"], [])
        except ProviderError:
            return
        raise AssertionError("expected ProviderError")


def test_openrouter_http_errors_map_to_provider_errors():
    with test_key():
        for code, fragment in ((401, "authentication"), (429, "rate limited"), (503, "temporary")):
            provider = OpenRouterProvider(
                model="x", api_key_env=TEST_KEY_ENV, urlopen_fn=error_transport(code))
            try:
                provider.classify(session(), "prompt")
            except ProviderError as exc:
                assert fragment in str(exc).casefold()
            else:
                raise AssertionError("expected ProviderError for HTTP {}".format(code))


def test_openrouter_network_and_malformed_errors():
    def unreachable(request, timeout):
        raise URLError("no route")

    with test_key():
        provider = OpenRouterProvider(
            model="x", api_key_env=TEST_KEY_ENV, urlopen_fn=unreachable)
        try:
            provider.classify(session(), "prompt")
        except ProviderError as exc:
            assert "could not reach" in str(exc)
        else:
            raise AssertionError("expected ProviderError")

        provider = OpenRouterProvider(
            model="x", api_key_env=TEST_KEY_ENV,
            urlopen_fn=ok_transport([], text="not json{{"))
        try:
            provider.classify(session(), "prompt")
        except ProviderError:
            pass
        else:
            raise AssertionError("expected ProviderError")


def test_openrouter_missing_key_fails_before_network():
    calls = []
    previous = os.environ.pop("NOEMA_TEST_MISSING_KEY", None)
    try:
        provider = OpenRouterProvider(
            model="x", api_key_env="NOEMA_TEST_MISSING_KEY",
            urlopen_fn=ok_transport(calls))
        try:
            provider.classify(session(), "prompt")
        except ProviderError as exc:
            assert "not set" in str(exc)
        else:
            raise AssertionError("expected ProviderError")
        assert calls == []
    finally:
        if previous is not None:
            os.environ["NOEMA_TEST_MISSING_KEY"] = previous


def test_chain_tries_openrouter_first_then_gemini_then_ollama():
    order = []

    class First:
        name = "openrouter"
        model = "minimax/minimax-m3:free"

        def classify(self, session, prompt):
            order.append("openrouter")
            return {
                "category": "productive", "productivity": "productive",
                "activity": "Work", "signal": "Evidence.",
            }

    class Never:
        name = "gemini"
        model = "gemini-x"

        def classify(self, session, prompt):
            order.append("gemini")
            raise AssertionError("must not be reached")

    ollama_calls = []

    class Tail:
        name = "ollama"
        model = "llama3.2:3b"

        def classify(self, session, prompt):
            ollama_calls.append(1)
            raise AssertionError("must not be reached")

    chain = ProviderChain(hosted=[Never()], ollama=Tail(), openrouter=[First()])
    assert chain.classify(object(), "prompt")["category"] == "productive"
    assert order == ["openrouter"]
    assert ollama_calls == []
    assert chain.last_provider == "openrouter"


def test_chain_falls_openrouter_to_gemini_to_ollama_to_pending():
    class Failing:
        def __init__(self, name, model):
            self.name = name
            self.model = model
            self.calls = 0

        def count_tokens(self, prompt):
            return 100

        def classify(self, session, prompt):
            self.calls += 1
            raise ProviderError("down")

    first = Failing("openrouter", "minimax/minimax-m3:free")
    second = Failing("gemini", "gemini-3.7-flash")
    tail = Failing("ollama", "llama3.2:3b")
    chain = ProviderChain(hosted=[second], ollama=tail, openrouter=[first], include_ollama=True)
    classifier = Classifier(provider=chain)

    result = classifier.classify(session(), "prompt")

    # Hosted tiers cool down after one failure each; the unlimited local
    # tail absorbs the classifier's bounded transport retries.
    assert first.calls == 1
    assert second.calls == 1
    assert tail.calls == 3
    assert result.classification_status == "pending"
    assert result.source == "pending"


def test_missing_openrouter_key_disables_tier_silently():
    previous = os.environ.pop("NOEMA_TEST_MISSING_KEY", None)
    try:
        config = DaemonConfig(openrouter_api_key_env="NOEMA_TEST_MISSING_KEY")
        assert _openrouter_providers(config) == []
    finally:
        if previous is not None:
            os.environ["NOEMA_TEST_MISSING_KEY"] = previous


def test_classifier_wires_openrouter_gemini_ollama_order():
    with test_key():
        config = DaemonConfig(
            provider="hosted",
            openrouter_api_key_env=TEST_KEY_ENV,
            openrouter_free_models=["minimax/minimax-m3:free"],
        )
        classifier = _classifier(config)
        names = [provider.name for provider in classifier.provider.providers]
        assert names[0] == "openrouter"
        assert classifier.provider.providers[0].model == "minimax/minimax-m3:free"
        assert classifier.provider.include_ollama is True


def test_classifier_ollama_mode_stays_local_only():
    config = DaemonConfig(provider="ollama")
    classifier = _classifier(config)
    assert getattr(classifier.provider, "name", "") == "ollama"


def test_openrouter_source_provenance_is_preserved():
    class Direct:
        name = "openrouter"
        model = "minimax/minimax-m3:free"

        def classify(self, session, prompt):
            return {
                "category": "productive", "productivity": "productive",
                "activity": "Work", "signal": "Evidence.",
            }

    result = Classifier(provider=Direct()).classify(session())
    assert result.provider == "openrouter"
    assert result.source == "openrouter"
    assert result.classification_status == "classified"


def test_secret_never_leaks_into_telemetry_or_quotas():
    with test_key():
        config = DaemonConfig(provider="hosted", openrouter_api_key_env=TEST_KEY_ENV)
        classifier = _classifier(config)
        chain = classifier.provider
        dumped = json.dumps(chain.telemetry(), default=str)
        assert TEST_KEY not in dumped
        assert TEST_KEY_ENV in dumped or "openrouter" in dumped


def test_gemini_thinking_level_flows_to_sdk_and_retries_ambiguous():
    seen = []

    class FakeModels:
        def generate_content(self, model, contents, config=None):
            seen.append(getattr(config, "thinking_config", None))
            level = getattr(getattr(config, "thinking_config", None), "thinking_level", None)
            text = json.dumps({
                "category": "neutral", "productivity": "neutral",
                "activity": "Unclear", "signal": "Thin evidence either way.",
                "confidence": 0.2,
            })

            class Response:
                pass

            response = Response()
            response.text = text
            return response

    class FakeClient:
        models = FakeModels()

    provider = GeminiProvider(model="gemini-3.7-flash", client=FakeClient())
    payload = provider.classify(session(), "prompt")

    assert payload["category"] == "neutral"
    assert len(seen) == 2  # low first, then one medium retry
    assert seen[0] is not None and "LOW" in str(seen[0])
    assert seen[1] is not None and "MEDIUM" in str(seen[1])


def test_gemini_keeps_first_verdict_when_medium_retry_fails():
    calls = []

    class FlakyModels:
        def generate_content(self, model, contents, config=None):
            calls.append(1)
            if len(calls) == 1:
                class Response:
                    text = json.dumps({
                        "category": "neutral", "productivity": "neutral",
                        "activity": "Unclear", "signal": "Thin evidence.",
                        "confidence": 0.1,
                    })
                return Response()
            raise TimeoutError("slow")

    class FlakyClient:
        models = FlakyModels()

    provider = GeminiProvider(model="gemini-3.7-flash", client=FlakyClient())
    payload = provider.classify(session(), "prompt")

    assert payload["category"] == "neutral"
    assert len(calls) == 2


def test_config_openrouter_and_gemini_model_lists():
    config = DaemonConfig()
    assert config.openrouter_enabled is True
    assert config.openrouter_free_models == [
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    ]
    assert config.gemini_models == [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.8-flash",
    ]
    assert config.ollama_enabled is True


def test_config_model_lists_accept_comma_strings_and_env():
    config = DaemonConfig(openrouter_free_models="a:free, b:free")
    assert config.openrouter_free_models == ["a:free", "b:free"]
    previous = os.environ.get("OPENROUTER_FREE_MODELS")
    os.environ["OPENROUTER_FREE_MODELS"] = "x:free"
    try:
        assert DaemonConfig.from_environment().openrouter_free_models == ["x:free"]
    finally:
        if previous is None:
            os.environ.pop("OPENROUTER_FREE_MODELS", None)
        else:
            os.environ["OPENROUTER_FREE_MODELS"] = previous
    try:
        DaemonConfig(openrouter_free_models=[])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty model list")


def test_openrouter_registry_is_single_source_of_truth():
    from noema.infrastructure.providers import (
        FAST_DISTRACTION_MODEL,
        MODEL_CONTEXT_WINDOWS,
        MODEL_LIMITS,
        OPENROUTER_MODEL_REGISTRY,
        openrouter_registry,
    )

    ids = [entry["id"] for entry in OPENROUTER_MODEL_REGISTRY]
    assert tuple(ids) == tuple(ProviderChain.OPENROUTER_MODELS)
    assert "minimax/minimax-m3:free" not in ids
    assert "nvidia/nemotron-3-ultra:free" not in ids
    assert FAST_DISTRACTION_MODEL in ids
    fast_entries = [entry for entry in OPENROUTER_MODEL_REGISTRY if entry["fast_path"]]
    assert len(fast_entries) == 1
    assert fast_entries[0]["id"] == FAST_DISTRACTION_MODEL
    for entry in OPENROUTER_MODEL_REGISTRY:
        assert MODEL_LIMITS[entry["id"]] == entry["limits"]
        assert MODEL_CONTEXT_WINDOWS[entry["id"]] == entry["context_window"]
        assert entry["role"] and entry["purpose"]
        assert "embeddings" in entry["excludes"]
        assert entry["display_name"] and entry["vendor"]
        assert entry["provider"] == "openrouter"
        assert isinstance(entry["max_output"], int) and entry["max_output"] > 0
        assert isinstance(entry["priority"], int)
        assert entry["enabled"] is True
    # Chain only admits enabled generative-chat classification models,
    # ordered by registry priority; embeddings/rerankers can never enter.
    assert tuple(
        entry["id"]
        for entry in sorted(OPENROUTER_MODEL_REGISTRY, key=lambda item: item["priority"])
        if entry.get("enabled") and entry.get("normal_classification")
    ) == tuple(ProviderChain.OPENROUTER_MODELS)
    for model_id in ProviderChain.OPENROUTER_MODELS:
        lowered = model_id.casefold()
        assert "embed" not in lowered and "rerank" not in lowered
    # Copies, never the live objects.
    assert openrouter_registry() == list(OPENROUTER_MODEL_REGISTRY)
    assert openrouter_registry() is not OPENROUTER_MODEL_REGISTRY


def test_openrouter_400_and_404_carry_the_provider_body():
    def body_transport(code, body):
        def fake_urlopen(request, timeout):
            raise HTTPError(request.full_url, code, "error", {}, io.BytesIO(body))
        return fake_urlopen

    with test_key():
        provider = OpenRouterProvider(
            model="google/gemma-4-31b-it:free", api_key_env=TEST_KEY_ENV,
            urlopen_fn=body_transport(400, b'{"error":{"message":"response_format json_object unsupported"}}'))
        try:
            provider.classify(session(), "prompt")
        except ProviderError as exc:
            # The 400 is format-related, so the provider retries bare and
            # the bare attempt hits the same stub: the raised error must
            # still carry the original body for diagnosis.
            assert "400" in str(exc)
            assert "response_format" in str(exc)
        else:
            raise AssertionError("expected ProviderError for HTTP 400")

        provider = OpenRouterProvider(
            model="nvidia/nemotron-3-ultra:free", api_key_env=TEST_KEY_ENV,
            urlopen_fn=body_transport(404, b'{"error":{"message":"No endpoints found for model"}}'))
        try:
            provider.classify(session(), "prompt")
        except ProviderError as exc:
            assert "404" in str(exc)
            assert "No endpoints found" in str(exc)
        else:
            raise AssertionError("expected ProviderError for HTTP 404")


def test_openrouter_format_400_retries_once_without_response_format():
    calls = []

    def flaky_format(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8")))
        if "response_format" in calls[-1]:
            raise HTTPError(
                request.full_url, 400, "bad request", {},
                io.BytesIO(b'{"error":{"message":"response_format type json_object is not supported"}}'))
        return FakeResponse(json.dumps({
            "choices": [{"message": {"content": valid_text()}}],
        }).encode("utf-8"))

    with test_key():
        provider = OpenRouterProvider(
            model="google/gemma-4-31b-it:free", api_key_env=TEST_KEY_ENV,
            urlopen_fn=flaky_format)
        payload = provider.classify(session(), "classify this")
    assert payload["category"] == "productive"
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]
    # Identical model, prompt, and budget: only the parameter changed.
    assert calls[1]["model"] == calls[0]["model"]
    assert calls[1]["messages"] == calls[0]["messages"]


def test_openrouter_non_format_400_is_not_retried():
    calls = []

    def bad_request(request, timeout):
        calls.append(1)
        raise HTTPError(
            request.full_url, 400, "bad request", {},
            io.BytesIO(b'{"error":{"message":"max_tokens exceeds limit"}}'))

    with test_key():
        provider = OpenRouterProvider(
            model="google/gemma-4-31b-it:free", api_key_env=TEST_KEY_ENV,
            urlopen_fn=bad_request)
        try:
            provider.classify(session(), "prompt")
        except ProviderError as exc:
            assert "400" in str(exc)
            assert "max_tokens" in str(exc)
        else:
            raise AssertionError("expected ProviderError for HTTP 400")
    assert len(calls) == 1


def test_openrouter_503_and_network_retry_once_then_fallback():
    calls = []

    def then_ok(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 503, "overloaded", {}, io.BytesIO(b"{}"))
        return FakeResponse(json.dumps({
            "choices": [{"message": {"content": valid_text()}}],
        }).encode("utf-8"))

    with test_key():
        provider = OpenRouterProvider(
            model="x", api_key_env=TEST_KEY_ENV, urlopen_fn=then_ok)
        assert provider.classify(session(), "prompt")["category"] == "productive"
    assert len(calls) == 2


def test_openrouter_429_is_not_retried_immediately():
    calls = []

    def throttled(request, timeout):
        calls.append(1)
        raise HTTPError(request.full_url, 429, "limited", {}, io.BytesIO(b"{}"))

    with test_key():
        provider = OpenRouterProvider(
            model="google/gemma-4-26b-a4b-it:free", api_key_env=TEST_KEY_ENV,
            urlopen_fn=throttled)
        try:
            provider.classify(session(), "prompt")
        except ProviderError as exc:
            assert "429" in str(exc)
        else:
            raise AssertionError("expected ProviderError for HTTP 429")
    # No immediate retry: the chain falls through and the quota state
    # cools down instead of hammering a hot rate limit.
    assert len(calls) == 1


def test_chain_parks_404_models_and_logs_failures(caplog):
    import logging

    class NotFound:
        name = "openrouter"
        model = "nvidia/nemotron-3-ultra:free"

        def classify(self, session, prompt):
            raise ProviderError(
                'OpenRouter model unavailable (HTTP 404) {"error": "not found"}')

    class Next:
        name = "openrouter"
        model = "google/gemma-4-26b-a4b-it:free"

        def classify(self, session, prompt):
            return {
                "category": "productive", "productivity": "productive",
                "activity": "Work", "signal": "Evidence.",
            }

    chain = ProviderChain(hosted=[Next()], ollama=None, openrouter=[NotFound()])
    parked = chain.rate_limits[NotFound.model]
    with caplog.at_level(logging.INFO, logger="noema.providers"):
        payload = chain.classify(object(), "prompt")
    assert payload["category"] == "productive"
    assert parked.cooldown_until - __import__("time").monotonic() > 1500
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "MODEL_FAILED model=nvidia/nemotron-3-ultra:free status=404" in messages
    assert "FALLBACK model=llama3.2:3b" in messages or "FALLBACK model=" in messages
