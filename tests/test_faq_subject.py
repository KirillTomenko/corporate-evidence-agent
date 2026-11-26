from unittest.mock import Mock

from docx import Document
import pytest

import agent
import quality
from evidence import FILES, index_documents


QUESTION = "Где получить тестовый прибор ПочтаТест-8502 для проверки почтовых уведомлений?"
NOISE = "Порядок проверки сметы определяет руководитель."
RULE = "Прибор ПочтаТест-8502 разрешено выдавать сотрудникам."
ADVICE = RULE.replace("разрешено", "запрещено")


@pytest.mark.parametrize(("question", "rule", "status"), [
    (QUESTION, None, "FAQ"),
    ("Где получить калибратор ДемоК42?", None, "FAQ"),
    (QUESTION, RULE, "NEEDS_REVIEW"),
    (QUESTION, "Выдача приборов производится только после согласования.", "NEEDS_REVIEW"),
])
def test_named_object_faq(tmp_path, question, rule, status):
    folder = tmp_path / "corpus"
    folder.mkdir()
    doc = Document()
    for _ in range(4):
        doc.add_paragraph(NOISE)
    if rule:
        doc.add_paragraph(rule)
    doc.save(folder / FILES["ib-project"])
    index_documents(tmp_path)
    ticket = quality.submit(question, None, "Тест", "session", root=tmp_path)
    advice = ADVICE if question == QUESTION else "Калибратор выдаёт оператор."
    quality.approve(ticket, "test", advice, root=tmp_path)

    llm = Mock()
    result = agent.ask(question, llm, root=tmp_path)
    assert result["status"] == status
    assert result["calls"]["search"] <= 2 and result["calls"]["read"] <= 3
    llm.assert_not_called()
    if rule:
        assert result["answer"] is None
        assert rule in [r["text"] for r in result["evidence"]]
        if rule == RULE:
            assert "противоречие" in result["reason"]
    else:
        assert result["answer"] == advice and result["evidence"] == []
        if question == QUESTION:
            assert result["calls"]["read"] > 0  # Real FTS hits on «проверки».
