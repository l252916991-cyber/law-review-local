import json

from scripts.build_npc_corpus import TITLE_OVERRIDES, canonical_law_name, extract_document, requested_laws


def _line(text):
    return {"chars": [{"char": char} for char in text]}


def test_extracts_complete_articles_from_official_page_bundle():
    bundle = {
        "cutoff": "2023-11-13",
        "detail": {"data": {
            "bbbs": "fixture", "title": "中华人民共和国测试法", "gbrq": "2020-01-01",
            "sxrq": "2020-02-01", "content": {"title": "测试法", "children": [
                {"title": "第一条", "children": []}, {"title": "第二条", "children": []},
            ]},
        }},
        "pages": [{"areas": [{"lines": [
            _line("第一条"), _line("第一条内容。"), _line("－1－"),
            _line("第二条"), _line("第二条内容。"),
        ]}]}],
    }
    document = extract_document(bundle, ["测试法"], "today")
    assert document["article_count"] == 2
    assert [article["text"] for article in document["articles"]] == ["第一条\n第一条内容。", "第二条\n第二条内容。"]
    assert document["aliases"] == ["中华人民共和国测试法", "测试法"]


def test_extracts_amendment_whose_numbering_starts_above_one():
    bundle = {
        "cutoff": "2023-11-13",
        "detail": {"data": {
            "bbbs": "amendment", "title": "中华人民共和国宪法修正案（1993年）", "gbrq": "1993-03-29",
            "sxrq": "1993-03-29", "content": {"title": "修正案", "children": [
                {"title": "第三条", "children": []}, {"title": "第四条", "children": []},
            ]},
        }},
        "pages": [{"areas": [{"lines": [
            _line("第三条"), _line("第三条内容。"), _line("第四条"), _line("第四条内容。"),
        ]}]}],
    }
    document = extract_document(bundle, ["宪法修正案1993年"], "today")
    assert [article["article_id"] for article in document["articles"]] == ["3", "4"]
    assert "3_through_4" in document["completeness_check"]


def test_extracts_amendment_without_detail_tree_and_keeps_quoted_articles():
    bundle = {
        "cutoff": "2023-11-13",
        "detail": {"data": {
            "bbbs": "amendment-no-tree", "title": "中华人民共和国宪法修正案（2018年）",
            "gbrq": "2018-03-11", "sxrq": "2018-03-11", "content": None,
        }},
        "pages": [{"areas": [{"lines": [
            _line("第三十二条"), _line("修正内容一。"),
            _line("第三十三条"), _line("修正内容二，修改后的条文如下："),
            _line("第一百二十三条"), _line("被修正文内容。"),
        ]}]}],
    }
    document = extract_document(bundle, ["宪法修正案2018年"], "today")
    assert [article["article_id"] for article in document["articles"]] == ["32", "33"]
    assert "（被修正文条）第一百二十三条" in document["articles"][-1]["text"]


def test_law_names_come_from_questions_and_skip_existing(tmp_path):
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    rows = [
        {"task": "1-1", "question_id": "a", "question": "社会法劳动法第十二条的内容是什么？"},
        {"task": "1-1", "question_id": "b", "question": "宪法修正案1993年第五条的内容是什么？"},
        {"task": "2-1", "question_id": "c", "question": "不应读取"},
    ]
    (campaign / "inputs.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    assert requested_laws(campaign, []) == {
        "中华人民共和国劳动法": {"劳动法"},
        "中华人民共和国宪法修正案（1993年）": {"宪法修正案1993年"},
    }
    assert canonical_law_name("中华人民共和国劳动法") == "中华人民共和国劳动法"


def test_constitution_maps_to_official_consolidated_title():
    assert TITLE_OVERRIDES["中华人民共和国宪法"] == "中华人民共和国宪法（2018年修正文本）"
