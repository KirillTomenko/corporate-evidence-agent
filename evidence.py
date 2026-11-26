"""Локальный поиск по разрешённым регламентам DOCX."""

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile

from docx import Document
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError


ROOT = Path(__file__).resolve().parent
FILES = {
    "ib-project": "Проектная деятельность Дирекции ИБ.docx",
    "dbrct-project": "Проектная деятельность ДБРЦТ.docx",
    "ib-presale": "Пресейловая деятельность Дирекции ИБ.docx",
    "tech-project": "Организация проектной деятельности Тех дирекции.docx",
}


class EvidenceError(ValueError):
    pass


def source_bytes(root, doc_id):
    if doc_id not in FILES:
        raise EvidenceError("Документ отсутствует в списке разрешённых.")
    path = root / "corpus" / FILES[doc_id]
    if path.resolve() != path:
        raise EvidenceError(f"Путь корпуса перенаправлен: {path}")
    return path.read_bytes()


def fragments(container, prefix=""):
    paragraph = table = 0
    for block in container.iter_inner_content():
        if isinstance(block, Paragraph):
            paragraph += 1
            if block.text.strip():
                yield f"{prefix}p{paragraph}", block.text
        elif isinstance(block, Table):
            table += 1
            for row_num, row in enumerate(block.rows, 1):
                column = row.grid_cols_before + 1
                # Physical cells avoid repeating text across merged grid slots.
                for tc in row._tr.tc_lst:
                    cell_path = f"{prefix}t{table}/r{row_num}/c{column}/"
                    yield from fragments(_Cell(tc, block), cell_path)
                    column += tc.grid_span


@contextmanager
def database(root, *, initialize=False):
    path = root / "data" / "evidence.sqlite3"
    if initialize:
        path.parent.mkdir(exist_ok=True)
    elif not path.is_file():
        raise EvidenceError("Индекс отсутствует. Выполните python evidence.py index.")
    with closing(sqlite3.connect(path)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE" if initialize else "BEGIN")
        if initialize:
            db.execute("""CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                applicability TEXT NOT NULL DEFAULT 'unknown',
                applicability_basis TEXT)""")
            db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS fragments USING fts5(
                text, doc_id UNINDEXED, fragment_id UNINDEXED, locator UNINDEXED,
                tokenize='unicode61')""")
        yield db


def index_documents(root=ROOT):
    root = root.resolve()
    report = {}
    with database(root, initialize=True) as db:
        for doc_id, filename in FILES.items():
            try:
                raw = source_bytes(root, doc_id)
            except FileNotFoundError:
                db.execute("DELETE FROM fragments WHERE doc_id=?", (doc_id,))
                db.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
                report[filename] = "Файл отсутствует в corpus/"
                continue
            digest = hashlib.sha256(raw).hexdigest()
            previous = db.execute("SELECT sha256 FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            if previous and previous[0] == digest:
                report[filename] = "Уже в индексе"
                continue
            try:
                document = Document(BytesIO(raw))
            except (BadZipFile, KeyError, ValueError, XMLSyntaxError) as exc:
                raise EvidenceError(f"Не удалось прочитать DOCX: {filename}") from exc
            db.execute("DELETE FROM fragments WHERE doc_id=?", (doc_id,))
            db.execute("INSERT OR REPLACE INTO documents(doc_id, sha256) VALUES (?, ?)", (doc_id, digest))
            count = 0
            for locator, text in fragments(document):
                fragment_id = f"{doc_id}:{digest}:{locator}"
                db.execute("INSERT INTO fragments(text, doc_id, fragment_id, locator) VALUES (?,?,?,?)",
                           (text, doc_id, fragment_id, locator))
                count += 1
            report[filename] = f"Проиндексировано фрагментов: {count}"
    return report


SELECT = """SELECT f.fragment_id, f.doc_id, f.locator, f.text,
                   d.sha256, d.applicability, d.applicability_basis
            FROM fragments AS f JOIN documents AS d USING(doc_id)"""


def verified_source(root, row):
    try:
        raw = source_bytes(root, row["doc_id"])
    except FileNotFoundError as exc:
        raise EvidenceError("Исходный файл отсутствует.") from exc
    if hashlib.sha256(raw).hexdigest() != row["sha256"]:
        raise EvidenceError("Контрольная сумма изменилась. Повторите индексацию.")
    return raw


def result(row):
    return dict(row) | {"filename": FILES[row["doc_id"]]}


def search_documents(query, limit=10, *, root=ROOT, match_any=False, doc_ids=None, prefix=False):
    if limit < 1:
        raise ValueError("Лимит должен быть положительным.")
    tokens = re.findall(r"[^\W_]+", query, re.UNICODE)
    if not tokens:
        return []
    operator = " OR " if match_any else " AND "
    suffix = "*" if prefix else ""
    match = operator.join(f'"{token}"{suffix}' for token in tokens)
    filters = " WHERE fragments MATCH ?"
    params = [match]
    if doc_ids is not None:
        if not doc_ids:
            return []
        filters += " AND f.doc_id IN (" + ",".join("?" for _ in doc_ids) + ")"
        params.extend(doc_ids)
    root = root.resolve()
    with database(root) as db:
        rows = db.execute(SELECT + filters + " ORDER BY bm25(fragments), f.fragment_id LIMIT ?",
                          [*params, limit])
        hits = []
        verified = set()
        for row in rows:
            if row["doc_id"] not in verified:
                verified_source(root, row)
                verified.add(row["doc_id"])
            hits.append(result(row))
        return hits


def read_evidence(fragment_id, *, root=ROOT):
    root = root.resolve()
    with database(root) as db:
        row = db.execute(SELECT + " WHERE f.fragment_id=?", (fragment_id,)).fetchone()
        if row is None:
            raise EvidenceError("Фрагмент отсутствует в текущем индексе.")
        raw = verified_source(root, row)
        for locator, text in fragments(Document(BytesIO(raw))):
            if locator == row["locator"] and text == row["text"]:
                if fragment_id == f"{row['doc_id']}:{row['sha256']}:{locator}":
                    return result(row)
        raise EvidenceError("Фрагмент по указанному локатору не подтверждён.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("index")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    commands.add_parser("read").add_argument("fragment_id")
    args = parser.parse_args()
    try:
        if args.command == "index":
            output = index_documents()
        elif args.command == "search":
            output = search_documents(args.query, args.limit)
        else:
            output = read_evidence(args.fragment_id)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
