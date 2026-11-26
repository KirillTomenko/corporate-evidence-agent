"""Локальный интерфейс поиска по корпоративным регламентам."""

import os
import time

import streamlit as st

import agent


UNKNOWN = "Актуальность и применимость регламента не подтверждены."
ERRORS = {
    "source": "Не удалось проверить источник. Проверьте локальный корпус и индекс.",
    "ollama": "Не удалось получить ответ модели. Проверьте доступность локальной Ollama и выбранной модели.",
}


def process(question):
    started = time.monotonic()
    try:
        client = agent.LocalOllama(os.getenv("CEA_OLLAMA_MODEL", "qwen2.5:1.5b"))
        result = agent.ask(question, client)
        if not result.get("error_kind"):
            return result
        message = ERRORS.get(result["error_kind"], "Не удалось завершить проверку.")
    except Exception:
        # Do not expose local paths, source data or exception details in the web UI.
        message = "Не удалось обработать вопрос. Проверьте настройки локального приложения."
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
    if status == "ERROR":
        st.error("Техническая ошибка")
        st.text(result["reason"])
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
            others = [r for r in rows if r is not cited]
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
        if not 1 <= len(question) <= 600:
            st.session_state.pop("last_result", None)
            st.warning("Введите вопрос длиной до 600 символов.")
        else:
            with st.spinner("Ищем и проверяем основания…"):
                st.session_state.last_result = (question, process(question))
    if "last_result" in st.session_state:
        show_result(*st.session_state.last_result)


if __name__ == "__main__":
    main()
