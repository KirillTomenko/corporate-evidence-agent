import json
import sqlite3
import sys
from unittest.mock import Mock

from docx import Document
import pytest
from streamlit.testing.v1 import AppTest

import agent
from evidence import FILES, index_documents
import quality
import quality_mail


Q = "Где получить калибратор?"
ANSWER = "Обратитесь в демонстрационную лабораторию."


def test_open_to_approved_and_exact_reuse(tmp_path):
    index_documents(tmp_path)
    llm = Mock()
    request = agent.ask(Q, llm, root=tmp_path)
    assert request["status"] == "NO_EVIDENCE"
    ticket = quality.submit(Q, None, request["reason"], "session", root=tmp_path)
    assert ticket == quality.submit(Q, None, request["reason"], "session", root=tmp_path)
    assert agent.ask(Q, llm, root=tmp_path)["status"] == "NO_EVIDENCE"
    quality.approve(ticket, "operator", ANSWER, "Основание №7", root=tmp_path)
    with pytest.raises(ValueError):
        quality.approve(ticket, "other", "Замена", root=tmp_path)
    with quality.store(tmp_path) as db:
        row = db.execute("SELECT * FROM tickets").fetchone()
        assert (row["status"], row["expert"], row["basis"]) == ("APPROVED", "operator", "Основание №7")
        assert row["approved_at"]
    result = agent.ask(Q, llm, root=tmp_path)
    assert result["status"] == "FAQ" and result["answer"] == ANSWER
    assert result["evidence"] == [] and not result.get("cited_fragment_id")
    assert "operator" not in str(result)
    assert agent.ask(Q.replace("получить", "сдать"), llm, root=tmp_path)["status"] == "NO_EVIDENCE"
    assert agent.ask(Q, llm, selected_doc="ib-project", root=tmp_path)["status"] == "NO_EVIDENCE"
    llm.assert_not_called()


@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.parametrize("conflict", [True, False])
def test_approved_faq_takes_priority(tmp_path, confirmed, conflict):
    directory = tmp_path / "corpus"
    directory.mkdir()
    doc = Document()
    claim = "Калибратор хранится на складе."
    doc.add_paragraph(claim)
    doc.save(directory / FILES["ib-project"])
    index_documents(tmp_path)
    if confirmed:
        with sqlite3.connect(tmp_path / "data/evidence.sqlite3") as db:
            db.execute("UPDATE documents SET applicability='confirmed', applicability_basis='test'")
    ticket = quality.submit(Q, None, "Тест", "a", root=tmp_path)
    advice = claim.replace("хранится", "не хранится") if conflict else claim
    quality.approve(ticket, "operator", advice, root=tmp_path)
    llm = Mock()
    result = agent.ask(Q, llm, root=tmp_path)
    assert result["status"] == "FAQ" and result["answer"] == advice
    assert result["reason"] == "Утверждённый ответ службы качества"
    assert result["evidence"] == [] and result["cited_fragment_id"] is None
    assert result["calls"] == {"search": 0, "read": 0, "llm": 0}
    llm.assert_not_called()


def test_action_only_matches_do_not_block_exact_faq(tmp_path):
    folder = tmp_path / "corpus"
    folder.mkdir()
    doc = Document()
    doc.add_paragraph("Получить согласование бюджета можно у руководителя.")
    doc.add_paragraph("Получить оценку подрядчика без договора запрещено.")
    doc.save(folder / FILES["ib-project"])
    index_documents(tmp_path)
    llm = Mock()
    scope = "ib-project"
    ticket = quality.submit(Q, scope, "Тест", "session", root=tmp_path)
    assert agent.ask(Q, llm, root=tmp_path, selected_doc=scope)["status"] == "NO_EVIDENCE"
    quality.approve(ticket, "operator", ANSWER, root=tmp_path)
    result = agent.ask(Q, llm, root=tmp_path, selected_doc=scope)
    assert result["status"] == "FAQ" and result["answer"] == ANSWER and not result["evidence"]
    assert agent.ask(Q, llm, root=tmp_path)["status"] == "NO_EVIDENCE"
    assert agent.ask(Q + " Срочно.", llm, root=tmp_path, selected_doc=scope)["status"] == "NO_EVIDENCE"
    llm.assert_not_called()


@pytest.mark.parametrize(("advice", "rule", "basis", "review"), [
    ("Выдавать прибор разрешено.", "Выдавать прибор разрешено.", "Тест", False),
    ("Выдавать прибор разрешено.", "Выдавать прибор запрещено.", "Тест", True),
    ("Выдавать прибор разрешено.", "Выдавать прибор запрещено.", "", False),
    ("Выдавать прибор через 3 дня разрешено.", "Выдавать прибор через 5 дней запрещено.", "Тест", False),
    ("Выдавать прибор разрешено.", "Выдавать прибор без заявки запрещено.", "Тест", False),
])
@pytest.mark.parametrize("selected", [True, False])
def test_scoped_faq_permission(tmp_path, advice, rule, basis, review, selected):
    scope = "dbrct-project"
    question = "Можно ли выдавать прибор?" if selected else "Можно ли выдавать прибор в ДБРЦТ?"
    folder = tmp_path / "corpus"
    folder.mkdir()
    doc = Document()
    doc.add_paragraph("1. " + rule)
    doc.save(folder / FILES[scope])
    index_documents(tmp_path)
    with sqlite3.connect(tmp_path / "data/evidence.sqlite3") as db:
        db.execute("UPDATE documents SET applicability='confirmed', applicability_basis=?", (basis,))
    selected_doc = scope if selected else None
    ticket = quality.submit(question, selected_doc, "Тест", "session", root=tmp_path)
    quality.approve(ticket, "demo", advice, root=tmp_path)
    result = agent.ask(question, Mock(side_effect=AssertionError), selected_doc=selected_doc, root=tmp_path)
    assert result["calls"] == {"search": 2, "read": 1, "llm": 0}
    assert result["cited_fragment_id"] is None
    if review:
        assert result["status"] == "NEEDS_REVIEW" and result["answer"] is None
        assert result["expert_answer"] == advice
        row, = result["evidence"]
        assert (row["text"], row["locator"], row["doc_id"]) == ("1. " + rule, "p1", scope)
    else:
        assert result["status"] == "FAQ" and result["answer"] == advice
        assert not result["evidence"]


def test_local_operator_commands(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(quality, "ROOT", tmp_path)
    ticket = quality.submit(Q, None, "Требуется консультация", "demo", root=tmp_path)
    def run(*args):
        monkeypatch.setattr(sys, "argv", ["quality.py", *args])
        quality.main()
        return capsys.readouterr().out
    assert json.loads(run("list"))[0]["question"] == Q
    assert json.loads(run("show", str(ticket)))[0]["status"] == "OPEN"
    assert "APPROVED" in run("approve", str(ticket), "--expert", "demo", "--answer", ANSWER)
    assert json.loads(run("list")) == []
    assert json.loads(run("show", str(ticket)))[0]["answer"] == ANSWER


def test_limits_and_database_errors(tmp_path):
    index_documents(tmp_path)
    for i in range(5):
        quality.submit(f"Вопрос {i}", None, "Тест", "a", root=tmp_path)
    with pytest.raises(quality.QueueLimit):
        quality.submit("Ещё вопрос", None, "Тест", "a", root=tmp_path)
    (tmp_path / "data/quality.sqlite3").write_bytes(b"damaged")
    assert agent.ask(Q, Mock(), root=tmp_path)["status"] == "ERROR"


def test_streamlit_quality_roundtrip(monkeypatch, tmp_path):
    index_documents(tmp_path)
    original_ask, original_submit = agent.ask, quality.submit
    monkeypatch.setattr(agent, "ask", lambda *a, **kw: original_ask(*a, **kw, root=tmp_path))
    submit = Mock(side_effect=lambda *args: original_submit(*args, root=tmp_path))
    monkeypatch.setattr(quality, "submit", submit)
    monkeypatch.delenv("CEA_SMTP_ENABLED", raising=False)
    original_notify = quality_mail.notify
    notify = Mock(side_effect=lambda ticket: original_notify(ticket, root=tmp_path))
    monkeypatch.setattr(quality_mail, "notify", notify)
    page = AppTest.from_file(str(agent.ROOT / "app.py")).run()
    page.text_area[0].set_value(Q)
    page.button[0].click().run()
    page.button[1].click().run()
    page.run()
    submit.assert_called_once()
    notify.assert_called_once()
    assert quality_mail.MESSAGES["DISABLED"] in [t.value for t in page.text]
    assert not page.exception and "ещё не получен" in page.success[0].value
    quality.approve(page.session_state["ticket"], "operator", ANSWER, root=tmp_path)
    page.button[0].click().run()
    assert not page.exception and page.info[0].value == "Утверждённый ответ службы качества"
    assert ANSWER in [text.value for text in page.text] and not page.expander
