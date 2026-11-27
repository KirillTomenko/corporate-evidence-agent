from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

import agent
from app import UNKNOWN


def test_conflicting_faq_offers_escalation(monkeypatch):
    row = {"filename": "Учебный.docx", "locator": "p1", "text": "Выдавать прибор запрещено.",
           "applicability": "confirmed", "fragment_id": "demo"}
    result = {"status": "NEEDS_REVIEW", "reason": "Возможно противоречие с регламентом.",
              "expert_answer": "Выдавать прибор разрешено.", "elapsed_seconds": 0, "evidence": [row]}
    monkeypatch.setattr(agent, "ask", Mock(return_value=result))
    page = AppTest.from_file(str(agent.ROOT / "app.py")).run()
    page.text_area[0].set_value("Можно ли выдавать прибор?")
    page.button[0].click().run()
    assert not page.exception and page.warning[0].value == result["reason"]
    texts = [item.value for item in page.text]
    assert texts.index(result["expert_answer"]) < texts.index("Учебный.docx · p1") < texts.index(row["text"])
    assert any(button.label == "Передать в службу качества" for button in page.button)


@pytest.mark.parametrize(("status", "widget", "label"), [
    ("ANSWERED", "success", "Ответ подтверждён"),
    ("NEEDS_REVIEW", "warning", "Требуется проверка"),
    ("NO_EVIDENCE", "info", "службу качества"),
    ("ERROR", "error", "Техническая ошибка"),
])
def test_result_and_rerun(monkeypatch, status, widget, label):
    rows = [{"fragment_id": str(i), "filename": "Пример.docx", "locator": f"p{i}",
             "text": f"Фрагмент {i}", "applicability": "unknown" if status == "NEEDS_REVIEW" else "confirmed"}
            for i in (1, 2)]
    result = {"status": status, "reason": "Локальная причина", "answer": "Ответ из второго пункта",
              "elapsed_seconds": 1.5, "evidence": rows, "cited_fragment_id": "2"}
    if status == "NO_EVIDENCE":
        result.update(evidence=[], cited_fragment_id=None, answer=None)
    if status == "ERROR":
        result.update(status="NEEDS_REVIEW", reason="private/path/to/file", error_kind="ollama")
    ask = Mock(return_value=result)
    monkeypatch.setattr(agent, "ask", ask)
    page = AppTest.from_file(str(agent.ROOT / "app.py")).run()
    page.text_area[0].set_value("Вопрос")
    page.button[0].click().run()
    page.run()
    ask.assert_called_once()
    assert not page.exception
    assert label in getattr(page, widget)[0].value
    texts = [item.value for item in page.text]
    if status in ("ANSWERED", "NEEDS_REVIEW"):
        assert texts.index("Фрагмент 2") < texts.index("Фрагмент 1")
        assert result["answer"] in texts
    else:
        assert result["answer"] not in texts and "private/path/to/file" not in texts
    if status == "NEEDS_REVIEW":
        assert UNKNOWN in texts
