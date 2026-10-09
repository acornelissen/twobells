import io
import json

from departureboard import http


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def capture(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(http, "urlopen", fake_urlopen)
    return seen


def test_sends_a_named_user_agent(monkeypatch):
    # The Rail Data Marketplace gateway returns 403 to urllib's default agent.
    seen = capture(monkeypatch)
    http.get_json("https://example.test/x")
    agent = seen["request"].get_header("User-agent")
    assert agent and agent.startswith("inky-departures/")
    assert "Python-urllib" not in agent


def test_keeps_caller_headers_and_asks_for_json(monkeypatch):
    seen = capture(monkeypatch)
    assert http.get_json("https://example.test/x", {"x-apikey": "k"}) == {"ok": True}
    request = seen["request"]
    assert request.get_header("X-apikey") == "k"
    assert request.get_header("Accept") == "application/json"
