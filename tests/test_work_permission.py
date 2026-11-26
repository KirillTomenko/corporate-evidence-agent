from unittest.mock import Mock

from docx import Document

import agent
from evidence import FILES, index_documents


QUESTION = "Можно ли начинать работы до подписания договора?"
DOC = "dbrct-project"
ESTIMATE = ("После подписания договора РП создаёт смету в течение трёх рабочих дней. "
            "В случае работы по письму смета не заводится до подписания договора.")
RULE = "До заключения контракта запрещено приступать к проектным работам."
PASS = {"supported": True, "responsive": True, "conflict": False}


def index(root, *paragraphs):
    folder = root / "corpus"
    folder.mkdir()
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.save(folder / FILES[DOC])
    index_documents(root)


def test_estimate_and_incidental_work_are_not_authorization(tmp_path):
    index(tmp_path, ESTIMATE, "До подписания договора проверяют смету, а после оплаты работы разрешены.",
          "До начала работ расчёт сметы направляется на согласование.")
    llm = Mock()
    result = agent.ask(QUESTION, llm, root=tmp_path, selected_doc=DOC)
    assert result["status"] == "NO_EVIDENCE" and result["answer"] is None
    assert "прямого правила" in result["reason"]
    llm.assert_not_called()


def test_clarified_permission_and_estimate_use_different_evidence(tmp_path):
    index(tmp_path, ESTIMATE, RULE)
    assert agent.ask(QUESTION, Mock(), root=tmp_path)["status"] == "CLARIFY"
    llm = Mock(side_effect=[{"claim": RULE, "source": 1}, PASS])
    result = agent.ask(QUESTION, llm, root=tmp_path, selected_doc=DOC)
    payload = llm.call_args_list[0].args[1]
    assert payload["question"] == QUESTION and payload["selected_document"] == FILES[DOC]
    assert [s["text"] for s in payload["sources"]] == [RULE]
    assert result["status"] == "NEEDS_REVIEW" and RULE in result["answer"]
    assert result["cited_fragment_id"] == result["evidence"][0]["fragment_id"]
    llm = Mock(side_effect=[{"claim": ESTIMATE, "source": 1}, PASS])
    estimate = agent.ask("Когда РП создаёт смету в ДБРЦТ?", llm, root=tmp_path)
    assert ESTIMATE in estimate["answer"]


def test_too_many_provisions_stop_before_reading(tmp_path):
    index(tmp_path, *(RULE + f" Исключение для группы {i}." for i in range(4)))
    result = agent.ask(QUESTION, Mock(), root=tmp_path, selected_doc=DOC)
    assert result["status"] == "NEEDS_REVIEW" and result["answer"] is None
    assert result["calls"] == {"search": 2, "read": 0, "llm": 0}


def test_linked_approval_steps_are_in_answer_and_verifier_context(tmp_path):
    request = "3.5. При отсутствии письма запрос на старт работ до подписания договора передают комиссии."
    approval = "3.6. Комиссия согласует старт работ. Работы могут быть начаты только после её решения."
    index(tmp_path, ESTIMATE, "3.4. " + RULE, request, approval)
    llm = Mock(side_effect=[{"claim": RULE, "source": 1}, PASS])
    result = agent.ask(QUESTION, llm, root=tmp_path, selected_doc=DOC)
    verification = llm.call_args.args[1]
    assert verification["supporting_sources"] == [2, 3]
    assert request in verification["claim"] and approval in verification["claim"]
    assert result["supporting_fragment_ids"] == [r["fragment_id"] for r in result["evidence"][1:]]
    assert result["status"] == "NEEDS_REVIEW" and approval in result["answer"]
