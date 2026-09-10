from app.benchmark_postprocess import postprocess, preserve_correction_surface


def test_preserves_lexical_fix_but_restores_source_surface():
    question = "在此情况下,各别部门未经公司同意且未与被告协商,自行改为2012年12月31日"
    prediction = "在此情况下，各别部门未经公司同意且未与被告协商，自行改为 2012 年 12 月 31 日。"
    assert preserve_correction_surface(question, prediction) == "在此情况下,各别部门未经公司同意且未与被告协商,自行改为2012年12月31日"


def test_uses_only_question_and_does_not_touch_other_tasks():
    prediction, audit = postprocess("2-1", "句子:错另名,金额100元", "错另明，金额 100 元。")
    assert prediction == "错另明,金额100元"
    assert audit["applied"] and audit["original_prediction"].endswith("。")
    assert postprocess("3-8", "anything", " keep me ")[0] == " keep me "


def test_preserves_spaces_when_source_uses_them():
    assert preserve_correction_surface("甲 乙，丙。", "甲 乙,丙。") == "甲 乙，丙。"


def test_exact_article_uses_retrieved_content_without_heading():
    retrieval = {"mode": "exact_article", "hits": [{
        "document_id": "law-2020", "article_id": "42", "text": "第四十二条 官方正文。",
    }]}
    prediction, audit = postprocess("1-1", "某法第四十二条", "模型幻觉", retrieval)
    assert prediction == "官方正文。"
    assert audit["applied"] and audit["document_articles"] == ["law-2020/42"]


def test_exact_article_repairs_archived_page_markup_surface():
    retrieval = {"mode": "exact_article", "hits": [{
        "document_id": "law", "article_id": "1", "text": "第一条 公司成立后,股东不得抽 逃出资。",
    }]}
    prediction, audit = postprocess("1-1", "某法第一条", "幻觉", retrieval)
    assert prediction == "公司成立后，股东不得抽逃出资。"
    assert audit["applied"] and audit["policy"] == "exact-retrieved-article-content; no reference access"


def test_trigger_words_are_deduplicated_and_follow_source_order():
    prediction, audit = postprocess("2-10", "补助款随后转账", "转账;补助款;转账")
    assert prediction == "补助款;转账" and audit["applied"]


def test_event_labels_are_extracted_from_source_language():
    prediction, audit = postprocess("2-9", "被告通过微信向他人购买药品", "买入")
    assert prediction == "买入;联络"
    assert audit["event_labels"] == ["买入", "联络"]


def test_news_summary_uses_bounded_source_lead_and_strips_boilerplate():
    question = (
        "【版权及免责声明：本文仅作分享之用。】"
        "第一句说明核心事件及主要主体，内容需要保留。"
        "第二句补充案件结果，也需要保留。"
        "第三句继续补充关键事实，长度仍未达到目标。"
        "第四句达到固定长度边界并保留完整句子，不截断事实。"
        "第五句属于边界之外，不应进入摘要。"
        "原标题：无关尾注"
    )
    prediction, audit = postprocess("2-7", question, "模型生成的摘要")
    assert prediction.startswith("第一句说明核心事件")
    assert "第四句" in prediction
    assert "第五句" not in prediction
    assert "免责声明" not in prediction
    assert audit["policy"] == "bounded-source-lead-extraction; no reference access"
    assert audit["target_characters"] == 120
    assert audit["max_sentences"] == 4
