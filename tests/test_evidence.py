import hashlib
import sqlite3

import pytest
from docx import Document

from evidence import FILES, EvidenceError, index_documents, read_evidence, search_documents


@pytest.fixture
def corpus(tmp_path):
    directory = tmp_path / "corpus"
    directory.mkdir()
    path = directory / FILES["ib-project"]
    doc = Document()
    doc.add_paragraph("Первый маркер")
    doc.add_paragraph("")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Общий маркер"
    table.cell(1, 1).text = "Табличный маркер"
    doc.add_paragraph("Последний маркер")
    doc.save(path)
    (directory / "Посторонний.docx").write_bytes(b"ignored")
    return tmp_path, path


def test_index_order_and_identity(corpus):
    root, path = corpus
    report = index_documents(root)
    assert sum("отсутствует" in msg for msg in report.values()) == 3
    hits = search_documents("маркер", root=root)
    assert len(hits) == 4
    assert all(hit["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest() for hit in hits)
    assert all(hit["applicability"] == "unknown" for hit in hits)
    assert [read_evidence(h["fragment_id"], root=root) for h in hits] == hits
    with sqlite3.connect(root / "data/evidence.sqlite3") as db:
        assert db.execute("SELECT locator FROM fragments ORDER BY rowid").fetchall() == [
            ("p1",), ("t1/r1/c1/p1",), ("t1/r2/c2/p1",), ("p3",)]
    index_documents(root)
    assert search_documents("маркер", root=root) == hits


def test_search(corpus):
    root, _ = corpus
    index_documents(root)
    hit, = search_documents("ТАБЛИЧНЫЙ маркер", root=root)
    assert hit["text"] == "Табличный маркер"
    assert hit["filename"] == FILES["ib-project"]
    assert search_documents("отсутствующий", root=root) == []
    assert search_documents('***"', root=root) == []
    with pytest.raises(EvidenceError, match="Фрагмент отсутствует"):
        read_evidence("../../invalid", root=root)


def test_changed_and_removed(corpus):
    root, path = corpus
    index_documents(root)
    old = search_documents("первый", root=root)[0]["fragment_id"]
    doc = Document()
    doc.add_paragraph("Новая версия")
    doc.save(path)
    with pytest.raises(EvidenceError, match="Контрольная сумма"):
        read_evidence(old, root=root)
    with pytest.raises(EvidenceError, match="Контрольная сумма"):
        search_documents("первый", root=root)
    index_documents(root)
    with pytest.raises(EvidenceError, match="Фрагмент отсутствует"):
        read_evidence(old, root=root)
    assert search_documents("первый", root=root) == []
    new = search_documents("версия", root=root)[0]["fragment_id"]
    path.unlink()
    with pytest.raises(EvidenceError, match="файл отсутствует"):
        read_evidence(new, root=root)
    index_documents(root)
    assert search_documents("версия", root=root) == []


def test_empty_corpus(tmp_path):
    report = index_documents(tmp_path)
    assert len(report) == 4
    assert all("отсутствует" in msg for msg in report.values())
    assert search_documents("запрос", root=tmp_path) == []
