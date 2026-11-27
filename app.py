"""Локальный интерфейс поиска по корпоративным регламентам."""

import os
import sqlite3
import threading
import time
import uuid

import streamlit as st

import agent
import quality
import quality_mail


UNKNOWN = "Актуальность и применимость регламента не подтверждены."
ERRORS = {
    "timeout": "Время обработки истекло. Повторите попытку позже.",
    "quality": "Не удалось проверить базу службы качества.",
    "source": "Не удалось проверить источник. Проверьте локальный корпус и индекс.",
    "ollama": "Не удалось получить ответ модели. Проверьте доступность локальной Ollama и выбранной модели.",
}

QUESTION_TIMEOUT = 60


@st.cache_resource
def agent_slot():
    return threading.BoundedSemaphore(1)


def process(question, selected_doc=None):
    started = time.monotonic()
    deadline = started + QUESTION_TIMEOUT
    slot = agent_slot()
    if not slot.acquire(blocking=False):
        return {"status": "BUSY", "reason": "Сервис обрабатывает другой запрос. Повторите попытку позже",
                "elapsed_seconds": time.monotonic() - started}
    try:
        client = agent.LocalOllama(os.getenv("CEA_OLLAMA_MODEL", "qwen2.5:1.5b"), deadline=deadline)
        result = agent.ask(question, client, selected_doc=selected_doc)
        if time.monotonic() >= deadline:
            raise TimeoutError
        if not result.get("error_kind"):
            return result
        message = ERRORS.get(result["error_kind"], "Не удалось завершить проверку.")
    except TimeoutError:
        message = ERRORS["timeout"]
    except Exception:
        # Do not expose local paths, source data or exception details in the web UI.
        message = "Не удалось обработать вопрос. Проверьте настройки локального приложения."
    finally:
        slot.release()
    return {"status": "ERROR", "reason": message, "elapsed_seconds": time.monotonic() - started}


def show_source(row):
    st.text(f"{row['filename']} · {row['locator']}")
    st.text(row["text"])
    if row["applicability"] == "unknown":
        st.caption(UNKNOWN)


def show_result(question, result):
    st.divider()
    st.subheader("Результат")
    st.text(question)
    status = result["status"]
    rows = result.get("evidence", [])
    cited = next((r for r in rows if r["fragment_id"] == result.get("cited_fragment_id")), None)
    if status == "BUSY":
        st.info(result["reason"])
    elif status == "ERROR":
        st.error("Техническая ошибка")
        st.text(result["reason"])
    elif status == "FAQ":
        st.info("Утверждённый ответ службы качества")
        st.text(result["answer"])
        if result["reason"] != "Утверждённый ответ службы качества":
            st.caption(result["reason"])
        st.caption("Экспертная консультация. Актуальность и применимость регламента требуют отдельной проверки.")
    elif status == "NEEDS_REVIEW" and "expert_answer" in result:
        st.warning(result["reason"])
        st.markdown("**Утверждённое разъяснение службы качества**")
        st.text(result["expert_answer"])
        st.markdown("**Проверенный фрагмент регламента**")
        show_source(rows[0])
        rows = []
    elif status == "NO_EVIDENCE":
        st.info("Достаточное основание не найдено. Рекомендуем обратиться в службу качества.")
    elif status == "ANSWERED" and cited and cited["applicability"] == "confirmed":
        st.success("Ответ подтверждён")
        st.text(result["answer"])
    else:
        st.warning("Требуется проверка")
        if cited and result.get("answer"):
            st.text(UNKNOWN)
            st.text(result["answer"])
        else:
            st.text(result["reason"])
    st.caption(f"Время обработки · {result['elapsed_seconds']:.2f} с")
    if rows and status != "ERROR":
        with st.expander("Проверяемые основания"):
            if cited:
                st.markdown("**Источник ответа**")
                show_source(cited)
            supporting = result.get("supporting_fragment_ids", [])
            if supporting:
                st.markdown("**Связанные условия, включённые в ответ**")
                for row in rows:
                    if row["fragment_id"] in supporting:
                        show_source(row)
            others = [r for r in rows if r is not cited and r["fragment_id"] not in supporting]
            if others:
                st.markdown("**Дополнительно просмотренные фрагменты**" if cited else "**Просмотренные фрагменты**")
                for row in others:
                    show_source(row)


def main():
    st.set_page_config(page_title="Corporate Evidence Agent", layout="centered")
    st.title("Corporate Evidence Agent")
    st.write("Задайте вопрос о корпоративном регламенте и сверьте ответ с текстом документа.")
    with st.form("question_form"):
        question = st.text_area("Вопрос сотрудника", max_chars=600,
                                placeholder="Когда РП создаёт смету в ДБРЦТ?")
        submitted = st.form_submit_button("Найти ответ", type="primary")
    if submitted:
        question = question.strip()
        st.session_state.pop("last_result", None)
        st.session_state.pop("scope_choice", None)
        st.session_state.clarifications = []
        st.session_state.quality_scope = None
        st.session_state.pop("ticket", None)
        st.session_state.pop("mail_notice", None)
        if not 1 <= len(question) <= 600:
            st.warning("Введите вопрос длиной до 600 символов.")
        else:
            with st.spinner("Ищем и проверяем основания…"):
                st.session_state.last_result = (question, process(question))
    if "last_result" in st.session_state:
        original, result = st.session_state.last_result
        if result["status"] == "CLARIFY":
            st.subheader("Уточните область вопроса")
            st.text(original)
            choices = {name.removesuffix(".docx"): key for key, name in agent.FILES.items()}
            with st.form("clarification"):
                choice = st.radio(result["reason"], [*choices, agent.UNSPECIFIED],
                                  index=None, key="scope_choice")
                resume = st.form_submit_button("Продолжить")
            if resume and choice is None:
                st.warning("Выберите область или укажите, что она неизвестна.")
            elif resume and not st.session_state.clarifications:
                st.session_state.clarifications.append((result["reason"], choice))
                if choice in choices:
                    st.session_state.quality_scope = choices[choice]
                    with st.spinner("Проверяем выбранный регламент…"):
                        result = process(original, choices[choice])
                else:
                    st.session_state.quality_scope = "unspecified"
                    result = dict(result, status="NEEDS_REVIEW",
                                  reason="Область вопроса осталась неопределённой. Уточните процесс у службы качества.")
                    try:
                        advice = quality.approved(original, "unspecified")
                        if advice is not None:
                            result.update(status="FAQ", answer=advice,
                                          reason="Область не определена. Сверка с регламентом не выполнена.")
                    except (sqlite3.Error, OSError):
                        result.update(status="ERROR", reason=ERRORS["quality"])
                st.session_state.last_result = (original, result)
                st.rerun()
        else:
            if st.session_state.clarifications:
                st.caption(f"Выбранная область · {st.session_state.clarifications[-1][1]}")
            show_result(original, result)
            if result["status"] in ("NO_EVIDENCE", "NEEDS_REVIEW") and not result.get("error_kind"):
                if "ticket" not in st.session_state and st.button("Передать в службу качества"):
                    if "quality_session" not in st.session_state:
                        st.session_state.quality_session = uuid.uuid4().hex
                    try:
                        st.session_state.ticket = quality.submit(
                            original, st.session_state.quality_scope, result["reason"],
                            st.session_state.quality_session)
                    except quality.QueueLimit as exc:
                        st.warning(str(exc))
                    except (sqlite3.Error, OSError, ValueError):
                        st.error("Не удалось зарегистрировать обращение. Попробуйте позже.")
                    else:
                        try:
                            state = quality_mail.notify(st.session_state.ticket)
                            st.session_state.mail_notice = quality_mail.MESSAGES[state]
                        except Exception:
                            st.session_state.mail_notice = "Состояние отправки письма не подтверждено. Обратитесь к оператору."
                if "ticket" in st.session_state:
                    st.success(f"Обращение №{st.session_state.ticket} зарегистрировано. "
                               "На момент передачи ответ эксперта ещё не получен.")
                    st.text(st.session_state.get("mail_notice", "Состояние отправки письма не подтверждено."))


if __name__ == "__main__":
    main()
