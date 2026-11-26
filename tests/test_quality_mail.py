import smtplib
import ssl
from unittest.mock import Mock

import pytest

import quality
import quality_mail as mail


@pytest.fixture
def smtp(monkeypatch):
    for key, value in dict(ENABLED="1", HOST="smtp.example.test", PORT="465",
                           LOGIN="operator", PASSWORD="test-only", FROM="agent@example.test",
                           TO="quality@example.test", CC="staff@example.test").items():
        monkeypatch.setenv("CEA_SMTP_" + key, value)
    connection = Mock()
    connection.return_value.send_message.return_value = {}
    monkeypatch.setattr(smtplib, "SMTP_SSL", connection)
    return connection


@pytest.fixture
def ticket(tmp_path):
    return quality.submit("Вопрос?\nBcc: outsider@example.test", None, "Скрытая причина", "user", root=tmp_path)


def test_envelope_and_no_replay(tmp_path, ticket, smtp):
    assert mail.notify(ticket, root=tmp_path) == "ACCEPTED"
    assert mail.notify(ticket, retry=True, root=tmp_path) == "ACCEPTED"
    smtp.assert_called_once()
    options = smtp.call_args.kwargs
    assert options["timeout"] <= 10
    assert options["context"].check_hostname and options["context"].verify_mode == ssl.CERT_REQUIRED
    send = smtp.return_value.send_message
    send.assert_called_once()
    assert send.call_args.kwargs["to_addrs"] == ["quality@example.test", "staff@example.test"]
    message = send.call_args.args[0]
    assert message["Bcc"] is None and str(ticket) in message["Subject"]
    assert "Вопрос?\nBcc: outsider@example.test" in message.get_content()
    assert "экспертом" in message.get_content() and "Скрытая причина" not in message.get_content()
    assert not message.is_multipart()


@pytest.mark.parametrize(("missing", "result"), [("ENABLED", "DISABLED"), ("PASSWORD", "CONFIG")])
def test_missing_settings(monkeypatch, tmp_path, ticket, smtp, missing, result):
    monkeypatch.delenv("CEA_SMTP_" + missing)
    assert mail.notify(ticket, root=tmp_path) == result
    smtp.assert_not_called()
    with quality.store(tmp_path) as db:
        assert db.execute("SELECT status FROM tickets WHERE id=?", (ticket,)).fetchone()[0] == "OPEN"


def test_operator_retry_only_after_known_failure(tmp_path, ticket, smtp):
    smtp.side_effect = ConnectionRefusedError()
    assert mail.notify(ticket, root=tmp_path) == "FAILED"
    smtp.side_effect = None
    assert mail.notify(ticket, root=tmp_path) == "FAILED"
    assert mail.notify(ticket, retry=True, root=tmp_path) == "ACCEPTED"
    assert smtp.call_count == 2
    with quality.store(tmp_path) as db:
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1


def test_second_attempt_during_smtp_is_blocked(tmp_path, ticket, smtp):
    def sending(*args, **kwargs):
        assert mail.notify(ticket, retry=True, root=tmp_path) == "SENDING"
        return {}
    smtp.return_value.send_message.side_effect = sending
    assert mail.notify(ticket, root=tmp_path) == "ACCEPTED"
    smtp.assert_called_once()


def test_recipient_must_be_one_admin_mailbox(monkeypatch, tmp_path, ticket, smtp):
    monkeypatch.setenv("CEA_SMTP_TO", "quality@example.test, outsider@example.test")
    assert mail.notify(ticket, root=tmp_path) == "CONFIG"
    smtp.assert_not_called()


@pytest.mark.parametrize("state", ["PARTIAL", "UNKNOWN"])
def test_no_replay_after_possible_acceptance(tmp_path, ticket, smtp, state):
    send = smtp.return_value.send_message
    if state == "UNKNOWN":
        send.side_effect = smtplib.SMTPServerDisconnected()
    else:
        send.return_value = {"staff@example.test": (550, b"refused")}
    assert mail.notify(ticket, root=tmp_path) == state
    assert mail.notify(ticket, retry=True, root=tmp_path) == state
    send.assert_called_once()
