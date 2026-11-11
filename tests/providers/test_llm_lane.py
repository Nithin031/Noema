"""External LLM lanes: priority, yielding to rivals, provider order, Claude Code argv."""

import json
import os
import subprocess
import threading
import time

import pytest

from noema.infrastructure.llm_lane import (
    ClaudeCodeProvider, HubClient, LaneBusy, LaneCoordinator, Priority,
    PriorityGate, WebLLMProvider, lane_priority,
)
from noema.infrastructure.providers import ProviderChain, ProviderError


def test_gate_serves_realtime_before_background():
    gate, order = PriorityGate(), []
    started = threading.Event()

    def worker(name, prio):
        with gate.acquire(prio, 5):
            order.append(name)

    with gate.acquire(Priority.BACKGROUND, 5):
        threads = [threading.Thread(target=worker, args=("bg", Priority.BACKGROUND)),
                   threading.Thread(target=worker, args=("rt", Priority.REALTIME))]
        threads[0].start(); time.sleep(0.05)
        threads[1].start(); time.sleep(0.05)
        started.set()
    for t in threads:
        t.join(5)
    assert order == ["rt", "bg"]


def test_gate_times_out_with_lanebusy():
    gate = PriorityGate()
    with gate.acquire(Priority.BACKGROUND, 5):
        with pytest.raises(LaneBusy):
            with gate.acquire(Priority.BACKGROUND, 0.05):
                pass


def test_fresh_rival_lease_means_busy_and_stale_does_not(tmp_path):
    coord = LaneCoordinator(tmp_path, rivals=("quaero",), stale_seconds=30)
    assert coord.rival_active() is None
    lease = tmp_path / "quaero.busy"
    lease.write_text("{}")
    assert coord.rival_active() == "quaero"
    old = time.time() - 120
    os.utime(lease, (old, old))
    assert coord.rival_active() is None


def test_noema_holds_its_own_lease_only_while_calling(tmp_path):
    coord = LaneCoordinator(tmp_path)
    with coord.hold("x"):
        assert (tmp_path / "noema.busy").exists()
    assert not (tmp_path / "noema.busy").exists()


class _Hub(HubClient):
    def __init__(self, busy=False, text='{"ok": true}'):
        super().__init__("http://127.0.0.1:1")
        self.busy, self.text, self.sent = busy, text, []

    def provider_busy(self, provider, max_age=2.0):
        return self.busy

    def chat(self, provider, prompt, timeout):
        self.sent.append((provider, prompt))
        return self.text


def _web(tmp_path, **kw):
    return WebLLMProvider("http://127.0.0.1:1", coordinator=LaneCoordinator(tmp_path),
                          client=_Hub(**kw), queue_wait=1)


def test_web_llm_yields_to_quaero_without_calling_the_hub(tmp_path):
    (tmp_path / "quaero.busy").write_text("{}")
    web = _web(tmp_path)
    assert web.lane_busy() is True
    with pytest.raises(LaneBusy):
        web.complete_json("p")
    assert web.client.sent == []


def test_web_llm_also_yields_when_the_hub_reports_busy(tmp_path):
    assert _web(tmp_path, busy=True).lane_busy() is True


def test_web_llm_returns_json_and_always_asks_for_a_fresh_chat(tmp_path):
    web = _web(tmp_path, text='```json\n{"a": 1}\n```')
    assert web.complete_json("hello") == {"a": 1}
    assert "ONLY the JSON" in web.client.sent[0][1]


def test_realtime_priority_is_tracked(tmp_path):
    from noema.infrastructure.llm_lane import current_priority
    assert current_priority() == Priority.BACKGROUND
    with lane_priority(Priority.REALTIME):
        assert current_priority() == Priority.REALTIME


def test_claude_code_uses_its_own_cwd_no_tools_no_history(tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, cwd=kw["cwd"], stdin=kw["input"])
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": '{"x": 2}', "is_error": False}), "")

    cc = ClaudeCodeProvider(workdir=tmp_path / "lane", runner=fake_run)
    assert cc.complete_json("hi") == {"x": 2}
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--effort") + 1] == "low"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv
    assert seen["cwd"] == str(tmp_path / "lane")


def test_claude_code_surfaces_usage_limit_as_a_provider_error(tmp_path):
    def fake_run(argv, **kw):
        body = {"is_error": True, "result": "You've hit your 5-hour limit"}
        return subprocess.CompletedProcess(argv, 1, json.dumps(body), "")

    with pytest.raises(ProviderError):
        ClaudeCodeProvider(workdir=tmp_path, runner=fake_run).complete_json("hi")


class _Gemini:
    name, model = "gemini", "gemini-x"

    def __init__(self):
        self.calls = 0

    def classify(self, session, prompt):
        self.calls += 1
        return {"category": "work"}


def test_chain_puts_external_lanes_first_and_skips_a_busy_one(tmp_path):
    (tmp_path / "quaero.busy").write_text("{}")
    web = _web(tmp_path)
    gemini = _Gemini()
    chain = ProviderChain(hosted=[gemini], external=[web], usage_path=str(tmp_path / "u.json"))
    assert [p.name for p in chain.providers] == ["web_llm", "gemini"]
    assert web.client.sent == []
    assert chain._lane_is_busy(web) is True
    assert chain._lane_is_busy(gemini) is False
