"""Уведомление о заявке через SMTP с TLS с момента подключения."""

from contextlib import closing
from email.headerregistry import Address
from email.errors import HeaderParseError
from email.message import EmailMessage
import os
import smtplib
import ssl

from quality import store
from evidence import ROOT


MESSAGES = {
    "DISABLED": "Письмо не отправлено: исходящая почта выключена.",
    "CONFIG": "Письмо не отправлено: требуется настройка исходящей почты.",
    "FAILED": "Письмо не отправлено: SMTP-сервер недоступен или отклонил запрос.",
    "SENDING": "Отправка начата. Подтверждение приёма письма пока отсутствует.",
    "UNKNOWN": "Получить подтверждение SMTP не удалось. Оператору следует проверить отправку.",
    "PARTIAL": "SMTP-сервер принял письмо только для одного из двух адресатов.",
    "ACCEPTED": "SMTP-сервер принял письмо для службы качества и адресата копии.",
    "CLOSED": "Обращение уже рассмотрено. Новое письмо не отправлено.",
}


def transmit(ticket, question):
    if os.getenv("CEA_SMTP_ENABLED") != "1":
        return "DISABLED"
    try:
        host, port, login, password, sender, recipient, copy = (
            os.environ["CEA_SMTP_" + key] for key in
            ("HOST", "PORT", "LOGIN", "PASSWORD", "FROM", "TO", "CC"))
        port = int(port)
        if not (host.strip() and login and password and 1 <= port <= 65535):
            return "CONFIG"
        message = EmailMessage()
        for key, value in (("From", sender), ("To", recipient), ("Cc", copy)):
            address = Address(addr_spec=value)
            if not address.domain or not address.username or not value.isascii() or address.addr_spec != value:
                return "CONFIG"
            message[key] = address
        if recipient.casefold() == copy.casefold():
            return "CONFIG"
    except (KeyError, ValueError, IndexError, HeaderParseError):
        return "CONFIG"
    message["Subject"] = f"Обращение в службу качества №{ticket}"
    message.set_content(f"Агент не смог дать подтверждённый ответ. Запрос требует рассмотрения экспертом.\n\n"
                        f"Обращение №{ticket}\nВопрос сотрудника:\n{question}")
    sending = False
    try:
        with closing(smtplib.SMTP_SSL(host, port, timeout=10, context=ssl.create_default_context())) as smtp:
            smtp.login(login, password)
            sending = True
            refused = smtp.send_message(message, from_addr=sender, to_addrs=[recipient, copy])
            return "PARTIAL" if refused else "ACCEPTED"
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
        return "FAILED"
    except (OSError, ValueError, smtplib.SMTPException):
        return "UNKNOWN" if sending else "FAILED"


def notify(ticket, *, retry=False, root=ROOT):
    with store(root, write=True) as db:
        row = db.execute("""SELECT t.question, t.status, m.state FROM tickets t
                            LEFT JOIN mail m ON t.id=m.ticket WHERE t.id=?""", (ticket,)).fetchone()
        if not row:
            raise ValueError("Обращение не найдено.")
        state = row["state"]
        if state and not (retry and state in ("DISABLED", "CONFIG", "FAILED")):
            return state
        if row["status"] != "OPEN":
            return "CLOSED"
        db.execute("INSERT OR REPLACE INTO mail(ticket,state) VALUES (?, 'SENDING')", (ticket,))
    # Commit the claim before network I/O. A crash leaves an uncertain attempt
    # blocked from automatic replay, even when another session submits again.
    state = transmit(ticket, row["question"])
    with store(root, write=True) as db:
        db.execute("UPDATE mail SET state=? WHERE ticket=?", (state, ticket))
    return state
