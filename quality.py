"""Локальная очередь службы качества и утверждённые консультации."""

import argparse
from contextlib import closing, contextmanager
import json
import sqlite3

from evidence import FILES, ROOT


class QueueLimit(ValueError):
    pass


def field(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise ValueError(f"Ожидается непустое поле длиной до {limit} символов.")
    return value


def match_key(question, scope):
    if scope not in (None, "unspecified") and scope not in FILES:
        raise ValueError("Неизвестная область.")
    # Preserve case, punctuation and internal whitespace (including identifiers).
    return field(question, 600).strip(), scope or ""


@contextmanager
def store(root, *, write=False):
    path = root / "data" / "quality.sqlite3"
    if write:
        path.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(path.as_uri() + ("?mode=rwc" if write else "?mode=ro"),
                                 uri=True, timeout=5)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        if write:
            db.execute("""CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY, session TEXT NOT NULL, question TEXT NOT NULL,
                scope TEXT NOT NULL, reason TEXT NOT NULL, created INTEGER NOT NULL DEFAULT (unixepoch()),
                status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','APPROVED')),
                answer TEXT, expert TEXT, approved_at TEXT, basis TEXT)""")
            db.execute("CREATE TABLE IF NOT EXISTS mail (ticket INTEGER PRIMARY KEY, state TEXT NOT NULL)")
        yield db


def submit(question, scope, reason, session, *, root=ROOT):
    key = match_key(question, scope)
    field(session, 64)
    field(reason, 800)
    with store(root, write=True) as db:
        existing = db.execute("""SELECT id FROM tickets WHERE question=? AND scope=? AND session=?
                                 AND status='OPEN'""", (*key, session)).fetchone()
        if existing:
            return existing["id"]
        count, own = db.execute("""SELECT count(*), count(*) FILTER (WHERE session=?) FROM tickets
                                   WHERE created >= unixepoch()-3600""", (session,)).fetchone()
        if count >= 60 or own >= 5:
            raise QueueLimit("Достигнут часовой лимит обращений. Попробуйте позже.")
        return db.execute("INSERT INTO tickets(question,scope,reason,session) VALUES (?,?,?,?)",
                          (*key, reason, session)).lastrowid


def approved(question, scope=None, *, root=ROOT):
    key = match_key(question, scope)
    try:
        (root / "data" / "quality.sqlite3").stat()
    except FileNotFoundError:
        return None
    with store(root) as db:
        row = db.execute("""SELECT answer FROM tickets WHERE question=? AND scope=? AND status='APPROVED'
                            ORDER BY approved_at DESC, id DESC LIMIT 1""", key).fetchone()
        return row["answer"] if row else None


def approve(ticket, expert, answer, basis=None, *, root=ROOT):
    field(expert, 100)
    field(answer, 3000)
    if basis is not None:
        field(basis, 500)
    with store(root, write=True) as db:
        changed = db.execute("""UPDATE tickets SET status='APPROVED', answer=?, expert=?, basis=?,
                                approved_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                                WHERE id=? AND status='OPEN'""", (answer, expert, basis, ticket))
        if changed.rowcount != 1:
            raise ValueError("Открытое обращение с таким номером не найдено.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Последние 100 открытых обращений")
    commands.add_parser("show").add_argument("id", type=int)
    commands.add_parser("retry-mail", help="Повторить неотправленное уведомление").add_argument("id", type=int)
    publish = commands.add_parser("approve", help="Утвердить текст для показа сотрудникам")
    publish.add_argument("id", type=int)
    publish.add_argument("--expert", required=True)
    publish.add_argument("--answer", required=True)
    publish.add_argument("--basis")
    args = parser.parse_args()
    try:
        if args.command == "retry-mail":
            from quality_mail import MESSAGES, notify
            state = notify(args.id, retry=True, root=ROOT)
            print(f"№{args.id} · {state}. {MESSAGES[state]}")
            return
        if args.command == "approve":
            approve(args.id, args.expert, args.answer, args.basis, root=ROOT)
            print(f"Обращение №{args.id}: APPROVED")
            return
        with store(ROOT, write=True) as db:
            columns = """SELECT id, question, scope, reason, status, answer, expert, approved_at, basis,
                         (SELECT state FROM mail WHERE ticket=tickets.id) AS mail_state FROM tickets"""
            if args.command == "show":
                rows = db.execute(columns + " WHERE id=?", (args.id,)).fetchall()
                if not rows:
                    raise ValueError("Обращение не найдено.")
            else:
                rows = db.execute(columns + " WHERE status='OPEN' ORDER BY id DESC LIMIT 100").fetchall()
            print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
