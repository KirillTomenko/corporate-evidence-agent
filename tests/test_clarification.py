from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

import agent


QUESTION = "Можно ли начинать работы до подписания договора?"


def test_clarification_costs_no_agent_calls():
    llm = Mock()
    result = agent.ask(QUESTION, llm)
    assert result["status"] == "CLARIFY"
    assert result["calls"] == {"search": 0, "read": 0, "llm": 0}
    llm.assert_not_called()


@pytest.mark.parametrize(("question", "scope"), [
    ("Когда РП создаёт смету в ДБРЦТ?", ["dbrct-project"]),
    (QUESTION + " В Дирекции ИБ.", ["ib-presale", "ib-project"]),
    (QUESTION + " Сравни ДБРЦТ и Тех дирекцию.", ["dbrct-project", "tech-project"]),
    (QUESTION + " Сравни разные регламенты.", None),
    ("Где находится журнал?", None),
])
def test_direct_search_scope(monkeypatch, question, scope):
    search = Mock(return_value=[])
    monkeypatch.setattr(agent, "search_documents", search)
    assert agent.ask(question, Mock())["status"] == "NO_EVIDENCE"
    assert search.call_count == 2
    assert all(c.kwargs["doc_ids"] == scope for c in search.call_args_list)


@pytest.mark.parametrize("unknown", [True, False])
def test_dialog_budget_and_reset(monkeypatch, unknown):
    ask = Mock(wraps=agent.ask)
    search = Mock(return_value=[])
    monkeypatch.setattr(agent, "ask", ask)
    monkeypatch.setattr(agent, "search_documents", search)
    page = AppTest.from_file(str(agent.ROOT / "app.py")).run()
    page.text_area[0].set_value(QUESTION)
    page.button[0].click().run()
    assert page.radio[0].value is None
    choice = agent.UNSPECIFIED if unknown else agent.FILES["ib-presale"].removesuffix(".docx")
    page.radio[0].set_value(choice)
    page.button[1].click().run()
    page.run()
    assert not page.exception and not page.radio
    original, result = page.session_state["last_result"]
    assert original == QUESTION
    assert page.session_state["clarifications"] == [(agent.CLARIFY, choice)]
    if unknown:
        assert ask.call_count == 1 and result["status"] == "NEEDS_REVIEW"
        search.assert_not_called()
    else:
        assert ask.call_count == 2 and result["status"] == "NO_EVIDENCE"
        assert ask.call_args.args[0] == QUESTION
        assert search.call_count == 2
        assert all(c.kwargs["doc_ids"] == ["ib-presale"] for c in search.call_args_list)
    page.text_area[0].set_value("Как начать работы по контракту?")
    page.button[0].click().run()
    assert page.radio[0].value is None and not page.session_state["clarifications"]
