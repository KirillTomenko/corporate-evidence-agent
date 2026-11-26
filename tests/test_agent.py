import sqlite3
from unittest.mock import Mock

import pytest
from docx import Document

import agent
from evidence import FILES, index_documents


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / "corpus"
    directory.mkdir()
    doc = Document()
    for subject in ("Журнал", "Акт", "Отчёт", "Протокол"):
        doc.add_paragraph(f"{subject} согласует руководитель.")
    doc.save(directory / FILES["ib-project"])
    index_documents(tmp_path)
    return tmp_path


QUESTION = "Кто согласует журнал?"
DRAFT = {"claim": "Журнал согласует руководитель.", "source": 1}
PASS = {"supported": True, "responsive": True, "conflict": False}


@pytest.mark.parametrize("confirmed", [False, True])
def test_grounded_answer(root, monkeypatch, confirmed):
    if confirmed:
        with sqlite3.connect(root / "data/evidence.sqlite3") as db:
            db.execute("UPDATE documents SET applicability='confirmed', applicability_basis='Тест'")
    search = Mock(wraps=agent.search_documents)
    read = Mock(wraps=agent.read_evidence)
    monkeypatch.setattr(agent, "search_documents", search)
    monkeypatch.setattr(agent, "read_evidence", read)
    llm = Mock(side_effect=[DRAFT, PASS])
    result = agent.ask(QUESTION, llm, root=root)
    assert result["status"] == ("ANSWERED" if confirmed else "NEEDS_REVIEW")
    assert DRAFT["claim"] in result["answer"]
    assert result["evidence"][0]["text"] == "Журнал согласует руководитель."
    assert result["evidence"][0]["locator"] == "p1"
    assert result["calls"] == {"search": 2, "read": 3, "llm": 2}
    assert (search.call_count, read.call_count, llm.call_count) == (2, 3, 2)
    assert result["elapsed_seconds"] >= 0


@pytest.mark.parametrize("verdict", [
    PASS | {"supported": False}, PASS | {"conflict": True}, {"supported": "true"},
    TimeoutError("timeout"),
])
def test_failed_verification(root, verdict):
    llm = Mock(side_effect=[DRAFT, verdict])
    result = agent.ask(QUESTION, llm, root=root)
    assert result["status"] == "NEEDS_REVIEW"
    assert result["answer"] is None
    assert llm.call_count == result["calls"]["llm"] == 2


def test_no_results(root):
    llm = Mock()
    result = agent.ask("Телепортация?", llm, root=root)
    assert result["status"] == "NO_EVIDENCE"
    assert result["calls"] == {"search": 1, "read": 0, "llm": 0}
    llm.assert_not_called()


@pytest.mark.parametrize(("neighbor", "conflict"), [
    ("Оператор архивирует реестр после проверки.", False),
    ("Оператор передаёт реестр через 5 рабочих дней.", True),
])
def test_deadline_and_neighbor(tmp_path, neighbor, conflict):
    directory = tmp_path / "corpus"
    directory.mkdir()
    doc = Document()
    rule = "Оператор передаёт реестр через 3 рабочих дня."
    doc.add_paragraph("7.2. " + rule)
    doc.add_paragraph(neighbor)
    doc.save(directory / FILES["ib-project"])
    index_documents(tmp_path)
    question = "Когда оператор передаёт реестр?"

    def client(instruction, payload):
        sources = payload["sources"]
        if "claim" not in payload:
            if not conflict:
                assert [s["text"] for s in sources] == ["7.2. " + rule]
            number = next(s["number"] for s in sources if s["locator"] == "p1")
            return {"claim": rule, "source": number}
        assert neighbor in [s["text"] for s in sources]
        return PASS | {"conflict": conflict}

    result = agent.ask(question, client, root=tmp_path)
    assert result["status"] == "NEEDS_REVIEW"
    if conflict:
        assert "противоречие" in result["reason"] and result["answer"] is None
    else:
        assert "Применимость" in result["reason"] and rule in result["answer"]
    number = next(i for i, s in enumerate(result["evidence"], 1) if s["locator"] == "p1")
    liar = Mock(side_effect=[{"claim": rule.replace("3 рабочих дня", "5 рабочих дней"),
                              "source": number}, PASS])
    rejected = agent.ask(question, liar, root=tmp_path)
    assert rejected["answer"] is None and "расходится" in rejected["reason"]
    liar.assert_called_once()


def test_string_in_boolean_field(root):
    verdict = PASS | {"responsive": DRAFT["claim"]}
    result = agent.ask(QUESTION, Mock(side_effect=[DRAFT, verdict]), root=root)
    assert "формат вердикта" in result["reason"]
    assert result["status"] == "NEEDS_REVIEW" and result["answer"] is None


def test_connection_error(root):
    llm = Mock(side_effect=ConnectionRefusedError("offline"))
    result = agent.ask(QUESTION, llm, root=root)
    assert result["status"] == "NEEDS_REVIEW"
    assert result["answer"] is None
    assert llm.call_count == result["calls"]["llm"] == 1


def test_remote_alias_rejected_before_sending_prompt(monkeypatch):
    client = agent.LocalOllama("alias:latest")
    post = Mock(return_value={"remote_host": "remote", "model_info": {"x": 1},
                              "details": {"format": "gguf"}})
    monkeypatch.setattr(client, "post", post)
    with pytest.raises(ValueError, match="GGUF"):
        client("instruction", {"question": "private"})
    post.assert_called_once_with("/api/show", {"model": "alias:latest"})


@pytest.fixture
def retrieval_root(tmp_path):
    directory = tmp_path / "corpus"
    directory.mkdir()
    for doc_id in ("dbrct-project", "ib-project"):
        doc = Document()
        for _ in range(8):
            doc.add_paragraph("Оператор передаёт уведомление.")
            doc.add_paragraph("Секретарь проверяет реестр.")
        doc.add_paragraph("После согласования оператор передаёт реестр.")
        doc.save(directory / FILES[doc_id])
    index_documents(tmp_path)
    return tmp_path


@pytest.mark.parametrize(("context", "expected"), [
    ("в ДБРЦТ", {"dbrct-project"}),
    ("в Дирекции ИБ", {"ib-project"}),
    ("", {"dbrct-project", "ib-project"}),
    ("в ДБРЦТ и Дирекции ИБ", {"dbrct-project", "ib-project"}),
])
def test_overlap_and_document_scope(retrieval_root, context, expected):
    calls = {"search": 0, "read": 0}
    rows = agent.retrieve(f"Когда оператор передаёт реестр {context}?", calls, root=retrieval_root)
    assert {row["doc_id"] for row in rows} == expected
    for row in rows[:len(expected)]:
        assert row["text"] == "После согласования оператор передаёт реестр."
    assert calls == {"search": 2, "read": 3}


def test_user_fts_syntax_is_literal(retrieval_root):
    calls = {"search": 0, "read": 0}
    rows = agent.retrieve('реестр" OR (NEAR*)', calls, root=retrieval_root)
    assert rows and all("реестр" in row["text"] for row in rows)
