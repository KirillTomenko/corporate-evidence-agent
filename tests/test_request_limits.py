from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

import agent


@pytest.mark.parametrize("failure", [TimeoutError("private/path"), RuntimeError("private/path")])
def test_two_sessions_and_release(monkeypatch, failure):
    entered = Event()
    release = Event()

    def ask(question, client, **kwargs):
        if question == "Первый":
            entered.set()
            assert release.wait(10)
            raise failure
        return {"status": "NO_EVIDENCE", "elapsed_seconds": 0.01}

    backend = Mock(side_effect=ask)
    monkeypatch.setattr(agent, "ask", backend)
    # AppTest owns a process-wide mock Runtime. Drive the first session's
    # handler in a worker while the second runs its normal form submission.
    script = "import streamlit as st\nfrom app import main, process\nst.session_state.handler = process\nmain()"
    first, second = [AppTest.from_string(script).run() for _ in range(2)]
    second.text_area[0].set_value("Второй")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(first.session_state["handler"], "Первый")
        try:
            assert entered.wait(5)
            second.button[0].click().run()
            assert second.info[0].value == "Сервис обрабатывает другой запрос. Повторите попытку позже"
            backend.assert_called_once()
        finally:
            release.set()
        failed = pending.result(timeout=10)
    assert failed["status"] == "ERROR" and "private/path" not in failed["reason"]
    second.button[0].click().run()
    second.run()
    assert not second.exception and backend.call_count == 2
    assert second.session_state["last_result"][1]["status"] == "NO_EVIDENCE"


def test_expired_deadline_prevents_next_http_call(monkeypatch):
    client = agent.LocalOllama("qwen2.5:1.5b", deadline=100)
    now = Mock(side_effect=[91.5, 100])
    monkeypatch.setattr(agent.time, "monotonic", now)
    connect = Mock()
    monkeypatch.setattr(agent.http.client, "HTTPConnection", connect)
    assert client.network_timeout() == 8.5
    with pytest.raises(TimeoutError):
        client.post("/api/generate", {})
    connect.assert_not_called()
