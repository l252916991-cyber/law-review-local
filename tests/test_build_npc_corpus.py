import json

from scripts.corpus.build_npc_corpus import TITLE_OVERRIDES, canonical_law_name, extract_document, requested_laws


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


def test_candidate_titles_try_both_prefix_forms():
    from scripts.corpus.build_npc_corpus import _candidate_titles

    # Administrative regulations are usually published without the prefix...
    assert _candidate_titles("工伤保险条例") == ["工伤保险条例", "中华人民共和国工伤保险条例"]
    # ...but a few carry it, and the prefixed input must still fall back.
    assert _candidate_titles("中华人民共和国户口登记条例") == [
        "中华人民共和国户口登记条例", "户口登记条例"]
    # Statutes keep the prefixed form first.
    assert _candidate_titles("中华人民共和国劳动法") == ["中华人民共和国劳动法", "劳动法"]
    assert _candidate_titles("中华人民共和国宪法")[0] == "中华人民共和国宪法（2018年修正文本）"


def test_repairs_headings_the_official_layout_merged_with_body_text():
    bundle = {
        "cutoff": "2023-11-13",
        "detail": {"data": {
            "bbbs": "merged", "title": "中华人民共和国测试法", "gbrq": "2020-01-01", "sxrq": "2020-02-01",
            "content": {"title": "测试法", "children": [
                {"title": "第一条", "children": []}, {"title": "第二条", "children": []},
            ]},
        }},
        "pages": [{"areas": [{"lines": [
            _line("第一条为了规范测试活动，制定本法。"),
            _line("第二条本法适用于测试活动的管理。"),
        ]}]}],
    }
    document = extract_document(bundle, ["测试法"], "today")
    assert document["layout_repair"] == "merged_heading_separator_v1"
    assert [article["heading"] for article in document["articles"]] == ["第一条", "第二条"]
    assert document["articles"][0]["text"].startswith("第一条 为了规范测试活动")


def test_repair_is_rejected_when_it_would_not_match_the_official_tree():
    bundle = {
        "cutoff": "2023-11-13",
        "detail": {"data": {
            "bbbs": "mismatch", "title": "中华人民共和国测试法", "gbrq": "2020-01-01", "sxrq": "2020-02-01",
            "content": {"title": "测试法", "children": [{"title": "第一条", "children": []},
                                                         {"title": "第二条", "children": []}]},
        }},
        # Only one merged heading exists; the repair cannot produce the second
        # article the official tree promises, so the publication must fail closed.
        "pages": [{"areas": [{"lines": [_line("第一条为了规范测试活动，制定本法。")]}]}],
    }
    try:
        extract_document(bundle, ["测试法"], "today")
    except ValueError as error:
        assert "Incomplete" in str(error)
    else:  # pragma: no cover - the assertion documents the required failure
        raise AssertionError("incomplete official publication must not be accepted")


def test_chinese_article_numbers_round_trip_through_the_shared_parser():
    from app.retrieval.legal_corpus import article_number
    from scripts.corpus.build_npc_corpus import _chinese_number

    for value in (1, 10, 12, 20, 100, 105, 110, 218, 999):
        assert article_number(_chinese_number(value)) == value


def test_breaks_article_headings_that_share_a_line_with_the_previous_article():
    bundle = {
        "cutoff": "2023-11-13",
        "detail": {"data": {
            "bbbs": "inline", "title": "中华人民共和国测试法", "gbrq": "2020-01-01", "sxrq": "2020-02-01",
            "content": {"title": "测试法", "children": [
                {"title": "第一条", "children": []}, {"title": "第二条", "children": []},
            ]},
        }},
        "pages": [{"areas": [{"lines": [
            _line("第一条为了规范测试活动，制定本法。第二条本法适用于测试活动的管理。"),
        ]}]}],
    }
    document = extract_document(bundle, ["测试法"], "today")
    assert document["layout_repair"] == "article_alignment_v1"
    assert [article["heading"] for article in document["articles"]] == ["第一条", "第二条"]


def test_inline_alignment_is_rejected_when_an_article_is_missing():
    bundle = {
        "cutoff": "2023-11-13",
        "detail": {"data": {
            "bbbs": "inline-missing", "title": "中华人民共和国测试法", "gbrq": "2020-01-01", "sxrq": "2020-02-01",
            "content": {"title": "测试法", "children": [
                {"title": "第一条", "children": []}, {"title": "第二条", "children": []},
                {"title": "第三条", "children": []},
            ]},
        }},
        "pages": [{"areas": [{"lines": [
            _line("第一条为了规范测试活动，制定本法。第二条本法适用于测试活动的管理。"),
        ]}]}],
    }
    try:
        extract_document(bundle, ["测试法"], "today")
    except ValueError as error:
        assert "Incomplete" in str(error)
    else:  # pragma: no cover
        raise AssertionError("an unreachable published article must fail closed")


def test_article_numbers_above_one_hundred_use_the_legal_spelling():
    from app.retrieval.legal_corpus import article_number
    from scripts.corpus.build_npc_corpus import _chinese_number

    assert _chinese_number(110) == "一百一十"
    assert _chinese_number(111) == "一百一十一"
    assert _chinese_number(210) == "二百一十"
    assert article_number("一百一十") == 110


def test_extracts_amendment_whose_clause_headings_are_merged_with_the_body():
    bundle = {
        "cutoff": "2023-11-13",
        "detail": {"data": {
            "bbbs": "amendment-merged", "title": "中华人民共和国宪法修正案（1988年）", "gbrq": "1988-04-12",
            "sxrq": "1988-04-12", "content": None,
        }},
        "pages": [{"areas": [{"lines": [
            _line("第一条宪法第十一条增加规定:“国家允许私营经济在法律规定的范围内存在和发展。"),
            _line("第二条宪法第十条第四款修改为:“任何组织或者个人不得侵占、买卖或者以其他形式非法转让土地。"),
        ]}]}],
    }
    document = extract_document(bundle, ["宪法修正案（1988年）"], "today")
    assert [article["heading"] for article in document["articles"]] == ["第一条", "第二条"]
    assert document["articles"][0]["text"].startswith("第一条 宪法第十一条增加规定")
    assert document["layout_repair"] == "merged_heading_separator_v1"
