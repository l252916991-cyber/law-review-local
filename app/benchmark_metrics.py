"""Deterministic scorers for the pinned LawBench and LexEval datasets.

Project-local metrics for LawBench e30981b, NOT the official leaderboard
evaluator (notably correction/RC/entity scores are local approximations).
Keep extraction independent of the current question's reference answer.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Iterable

import jieba
import cn2an

SCORER_VERSION = "lawbench-local-v3"


LAW_BENCH_METRICS = {
    "1-1": "rouge_l",
    "1-2": "accuracy",
    "2-1": "f0.5",
    "2-2": "accuracy",
    "2-3": "f1",
    "2-4": "accuracy",
    "2-5": "rc_f1",
    "2-6": "soft_f1",
    "2-7": "rouge_l",
    "2-8": "accuracy",
    "2-9": "f1",
    "2-10": "soft_f1",
    "3-1": "f1",
    "3-2": "rouge_l",
    "3-3": "f1",
    "3-4": "normalized_log_distance",
    "3-5": "normalized_log_distance",
    "3-6": "accuracy",
    "3-7": "accuracy",
    "3-8": "rouge_l",
}


@dataclass(frozen=True)
class ItemScore:
    score: float
    metric: str
    abstained: bool = False
    parsed_prediction: str | list[str] | float | None = None
    parsed_reference: str | list[str] | float | None = None
    parse_failed: bool = False
    reference_invalid: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_text(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def char_f1(reference: str, prediction: str) -> float:
    reference_tokens = list(normalize_text(reference))
    prediction_tokens = list(normalize_text(prediction))
    if not reference_tokens or not prediction_tokens:
        return float(reference_tokens == prediction_tokens)
    common = Counter(reference_tokens) & Counter(prediction_tokens)
    overlap = sum(common.values())
    if not overlap:
        return 0.0
    precision = overlap / len(prediction_tokens)
    recall = overlap / len(reference_tokens)
    return 2 * precision * recall / (precision + recall)


def set_f1(reference: Iterable[str], prediction: Iterable[str]) -> float:
    expected, actual = set(reference), set(prediction)
    if not expected:
        return float(not actual)
    if not actual:
        return 0.0
    overlap = len(expected & actual)
    precision, recall = overlap / len(actual), overlap / len(expected)
    return 2 * precision * recall / (precision + recall) if overlap else 0.0


def rouge_l(reference: str, prediction: str) -> float:
    """ROUGE-L F1 over jieba tokens, matching LawBench tokenization."""
    ref = list(jieba.cut(reference.strip()))
    pred = list(jieba.cut(prediction.strip()))
    if not ref or not pred:
        return float(ref == pred)
    previous = [0] * (len(pred) + 1)
    for left in ref:
        current = [0]
        for index, right in enumerate(pred, 1):
            current.append(previous[index - 1] + 1 if left == right else max(previous[index], current[-1]))
        previous = current
    lcs = previous[-1]
    precision, recall = lcs / len(pred), lcs / len(ref)
    return 2 * precision * recall / (precision + recall) if lcs else 0.0


def extract_option_set(text: str, allowed: str = "ABCDE") -> set[str]:
    """改进的选项提取函数，支持多种答案格式"""
    upper = text.upper()
    tagged = re.findall(r"\[正确答案\]\s*(.*?)\s*<EOA>", upper, re.S)
    if tagged:
        compact = re.sub(r"[\s,，、;；*]", "", tagged[-1])
        return set(compact) if compact and all(c in allowed for c in compact) else set()

    # 扩展的模式列表，按优先级排序
    patterns = (
        # 标准格式: 正确答案是：B / 正确答案：B
        rf"正确答案\s*(?:是|为)?\s*[:：]?\s*\*?\*?([{allowed}]+)\*?\*?",
        # [正确答案] B 格式
        rf"\[?正确答案\]?\s*[:：]?\s*([{allowed}]+)",
        # 答案：B / 选择：B 格式
        rf"(?:答案|选择|选项)\s*[:：]?\s*([{allowed}]+)",
        # 选B / 应选B 格式
        rf"(?:选|应选|答)\s*([{allowed}]+)",
        # 单个加粗选项 **B**
        rf"\*\*([{allowed}]+)\*\*",
        # B项正确 / B选项正确
        rf"([{allowed}]+)\s*(?:项|选项)?\s*(?:正确|符合)",
        # 第一行单独的选项字母
        rf"^([{allowed}]+)(?:\s|$|。|，)",
    )

    for pattern in patterns:
        match = re.search(pattern, upper)
        if match:
            extracted = match.group(1)
            # 过滤出有效的选项字母
            valid = set(c for c in extracted if c in allowed)
            if valid:
                return valid

    # 最后尝试：移除所有非选项字符，看剩下的是否都是有效选项
    compact = re.sub(r"[\s,，、;；。.<>{}()（）\[\]\n\r*]", "", upper)
    if compact and len(compact) <= 5 and all(char in allowed for char in compact):
        return set(compact)

    return set()


def extract_gold_options(reference: str) -> set[str]:
    values = extract_option_set(reference)
    if values:
        return values
    stripped = reference.strip().upper()
    return set(stripped) if stripped and all(char in "ABCDE" for char in stripped) else set()


def extract_labels(text: str, label_space: Iterable[str]) -> set[str]:
    """改进的标签提取函数，支持更灵活的匹配"""
    # Long labels first prevents a shorter label embedded in a longer one from
    # being counted as a second, spurious prediction.
    found: set[str] = set()
    occupied: list[tuple[int, int]] = []

    # 按长度降序排序标签，优先匹配较长的标签
    sorted_labels = sorted(set(label_space), key=len, reverse=True)

    for label in sorted_labels:
        # 精确匹配
        for match in re.finditer(re.escape(label), text):
            span = match.span()
            if not any(span[0] >= start and span[1] <= end for start, end in occupied):
                found.add(label)
                occupied.append(span)
                break

        # 如果没有精确匹配，尝试模糊匹配（去除空格、标点）
        if label not in found:
            # 规范化标签和文本用于模糊匹配
            normalized_label = re.sub(r'[\s、,，;；。]', '', label)
            normalized_text = re.sub(r'[\s、,，;；。]', '', text)

            if normalized_label in normalized_text:
                # 找到模糊匹配位置
                pos = normalized_text.find(normalized_label)
                # 估算原始文本中的位置（粗略）
                if pos >= 0 and not any(
                    pos >= start and pos + len(normalized_label) <= end
                    for start, end in occupied
                ):
                    found.add(label)
                    occupied.append((pos, pos + len(normalized_label)))

    # 如果仍然没有找到，尝试关键词匹配
    if not found and sorted_labels:
        for label in sorted_labels:
            # 提取标签中的关键词（超过2个字的词）
            keywords = [word for word in jieba.cut(label) if len(word) >= 2]
            if keywords:
                # 如果文本包含标签的大部分关键词，认为匹配
                matched = sum(1 for kw in keywords if kw in text)
                if matched >= len(keywords) * 0.6:  # 60%关键词匹配
                    found.add(label)

    return found


def split_reference_labels(task_id: str, reference: str) -> set[str]:
    """Parse labels using each task's declared output delimiter.

    In particular, LawBench 3-3 uses semicolons between charges.  Chinese
    commas/ideographic commas are part of many canonical charge names and
    therefore must never be treated as charge delimiters.
    """
    value = reference.strip()
    if task_id == "2-2":
        value = value.removeprefix("争议焦点类别：").removeprefix("争议焦点类别:").rstrip("。")
    elif task_id == "2-3":
        value = value.removeprefix("类别:").removeprefix("类别：").rstrip("。")
    elif task_id == "3-3":
        value = value.removeprefix("罪名:").removeprefix("罪名：")
    elif task_id == "3-1":
        return set(re.findall(r"\d+", value))
    if task_id == "2-3":
        return {part.strip() for part in re.split(r"[;；、]", value) if part.strip()}
    if task_id in {"2-9", "3-3"}:
        return {part.strip() for part in re.split(r"[;；]", value) if part.strip()}
    return {value} if value else set()


def extract_entities(text: str, entity_types: Iterable[str]) -> dict[str, str]:
    output: dict[str, str] = {}
    for entity_type in entity_types:
        match = re.search(rf"{re.escape(entity_type)}\s*[:：]\s*([^;；\n]+)", text)
        if match:
            value = match.group(1).strip()
            if value not in {"无", "未提及"}:
                output[entity_type] = value
    return output


def entity_f1(reference: str, prediction: str) -> float:
    expected_types = [
        part.split(":", 1)[0].split("：", 1)[0].strip()
        for part in re.split(r"[;；]", reference)
        if ":" in part or "：" in part
    ]
    expected = extract_entities(reference, expected_types)
    actual = extract_entities(prediction, ENTITY_TYPES)
    if not expected:
        return float(not actual)
    overlaps = [char_f1(expected[key], value) for key, value in actual.items() if key in expected]
    precision = sum(overlaps) / len(actual) if actual else 0.0
    recall = sum(overlaps) / len(expected)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def correction_f05(origin: str, reference: str, prediction: str) -> float:
    """Character-edit F0.5 compatible fallback for LawBench's ChERRANT task."""
    def edits(target: str) -> set[tuple[str, int, int, str]]:
        matcher = SequenceMatcher(a=origin, b=target)
        return {
            (tag, i1, i2, target[j1:j2])
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
            if tag != "equal"
        }

    gold, predicted = edits(reference), edits(prediction)
    if not gold:
        return float(not predicted)
    if not predicted:
        return 0.0
    overlap = len(gold & predicted)
    precision, recall = overlap / len(predicted), overlap / len(gold)
    beta2 = 0.25
    return (1 + beta2) * precision * recall / (beta2 * precision + recall) if precision + recall else 0.0


def origin_sentence(question: str) -> str:
    for marker in ("句子：\n", "句子:", "句子："):
        if marker in question:
            return question.split(marker, 1)[1].splitlines()[0].strip()
    return question.strip()


def extract_months(text: str) -> int | None:
    """提取刑期月数，支持阿拉伯数字和中文数字"""
    import cn2an

    # 先尝试阿拉伯数字的年月组合（最精确）
    month_match = re.search(r"(\d+)\s*年\s*(\d+)\s*个?月", text)
    if month_match:
        return int(month_match.group(1)) * 12 + int(month_match.group(2))

    # 阿拉伯数字纯月份
    month_match = re.search(r"(\d+)\s*个?月", text)
    if month_match:
        return int(month_match.group(1))

    # 阿拉伯数字纯年份
    year_match = re.search(r"(\d+)\s*年", text)
    if year_match:
        return int(year_match.group(1)) * 12

    # 中文数字：X年Y月（必须在纯年份之前尝试）
    match = re.search(r"([零一二三四五六七八九十百千]+)年\s*([零一二三四五六七八九十百千]+)\s*个?月", text)
    if match:
        try:
            years = cn2an.cn2an(match.group(1), 'smart')
            months = cn2an.cn2an(match.group(2), 'smart')
            return years * 12 + months
        except Exception:
            pass

    # 中文数字纯年份
    match = re.search(r"([零一二三四五六七八九十百千]+)年", text)
    if match:
        try:
            return cn2an.cn2an(match.group(1), 'smart') * 12
        except Exception:
            pass

    # 中文数字纯月份
    match = re.search(r"([零一二三四五六七八九十百千]+)\s*个?月", text)
    if match:
        try:
            return cn2an.cn2an(match.group(1), 'smart')
        except Exception:
            pass

    return None


ENTITY_TYPES = ("犯罪嫌疑人", "受害人", "被盗货币", "物品价值", "盗窃获利", "被盗物品", "作案工具", "时间", "地点", "组织机构")
NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)"


def answer_span(text: str, marker: str) -> str:
    """Prefer an explicit answer, never extract a matching gold from rationale."""
    tagged = re.findall(rf"\[{re.escape(marker)}\]\s*(.*?)\s*(?:<eoa>|$)", text, re.S | re.I)
    return tagged[-1].strip() if tagged else text.strip()


def number_value(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return float(cn2an.cn2an(value, "smart"))


def extract_articles(text: str) -> set[str]:
    value = answer_span(text, "法条")
    matches = re.findall(rf"第?\s*({NUMBER})\s*条", value)
    if not matches and re.fullmatch(r"[\d\s、,，;；]+", value):
        matches = re.findall(r"\d+", value)
    parsed = set()
    for n in matches:
        try:
            parsed.add(str(int(number_value(n))))
        except (ValueError, OverflowError):
            continue
    return parsed


def extract_final_amount(text: str) -> float | None:
    value = answer_span(text.replace("**", ""), "金额")
    value = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", value)
    amount = rf"({NUMBER})\s*(万|千)?\s*元?"
    # A final total takes precedence over component amounts in the explanation.
    totals = re.findall(rf"(?:犯罪总金额|犯罪金额|总金额|最终金额|金额合计|合计|总计|累计|共计)\s*(?:为|是)?\s*[:：]?\s*(?:人民币)?\s*{amount}", value)
    if totals:
        candidates = totals[-1:]
    else:
        match = re.fullmatch(rf"\s*(?:人民币)?\s*{amount}[。.]?\s*", value)
        candidates = [match.groups()] if match else []
    if not candidates:
        return None
    n, unit = candidates[0]
    try:
        result = number_value(n) * {"万": 10000, "千": 1000, "": 1}[unit or ""]
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError):
        return None


def extract_final_months(text: str) -> int | None:
    value = answer_span(text.replace("**", ""), "刑期")
    # Never interpret dates or a statutory range as a predicted sentence.
    duration = rf"(?:(?:{NUMBER})\s*年\s*)?(?:{NUMBER})\s*个?月|(?:{NUMBER})\s*年"
    assertions = re.findall(rf"(?:判处|判决刑期|刑期|有期徒刑|拘役)\s*(?:为|是)?\s*[:：]?\s*(?:有期徒刑|拘役)?\s*({duration})", value)
    if assertions:
        if len(set(assertions)) != 1:
            return None
        value = assertions[-1]
    elif not re.fullmatch(rf"(?:{duration})[。.]?", value):
        return None
    # Ranges are deliberately not converted into a single-point answer.
    if re.search(r"以上|以下|至|到|或|—|~|～", text) and "[刑期]" not in text:
        return None
    return extract_months(value)


def _normalized_label(task_id: str, value: str) -> str:
    """Normalize harmless presentation variants, not semantic paraphrases."""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"\s+", "", normalized.strip().strip("`*\"'“”‘’。.!！"))
    if task_id == "3-3":
        normalized = normalized.removesuffix("罪")
        normalized = normalized.replace(",", "、").replace("，", "、")
    return normalized


def parse_label_answer(task_id: str, text: str, label_space: Iterable[str] = ()) -> set[str]:
    """Parse the answer actually scored, independent of the item reference.

    Raw non-empty labels remain predictions even when they are outside the
    ontology: that is a wrong label, not a parser failure.  Recognized harmless
    variants are mapped back to the canonical global ontology label.
    """
    marker = {"2-2": "争议焦点", "2-3": "类别", "2-4": "类别", "2-9": "事件", "3-3": "罪名"}[task_id]
    value = answer_span(text.replace("**", ""), marker)
    value = re.sub(r"^(?:争议焦点类别|争议焦点|类别|罪名|事件)\s*[:：]\s*", "", value)
    separators = r"[;；、,，\n]" if task_id in {"2-3", "2-9"} else r"[;；\n]"
    raw_parts = [part.strip().rstrip("。.") for part in re.split(separators, value) if part.strip()]
    ontology = set(label_space)
    if not ontology:
        return {_normalized_label(task_id, part) for part in raw_parts if _normalized_label(task_id, part)}

    parsed: set[str] = set()
    normalized_ontology = {_normalized_label(task_id, label): label for label in sorted(ontology)}
    for part in raw_parts:
        normalized = _normalized_label(task_id, part)
        if not normalized:
            continue
        exact = normalized_ontology.get(normalized)
        if exact is not None:
            parsed.add(exact)
            continue
        # Do not mine substrings from rationale or an unknown compound charge.
        # For example, 合同诈骗 must not become 诈骗 if absent from the ontology.
        parsed.add(normalized)
    return parsed


@lru_cache(maxsize=5)
def task_label_space(task_id: str) -> frozenset[str]:
    from app.lawbench import load_task
    # Global evaluation ontology, never sent to the model and never narrowed to
    # the current reference. The official instruction supplies model-side labels.
    return frozenset(label for row in load_task(task_id) for label in split_reference_labels(task_id, row["answer"]))


def score_lawbench_item(
    task_id: str,
    prediction: str,
    reference: str,
    question: str = "",
    label_space: Iterable[str] = (),
) -> ItemScore:
    metric = LAW_BENCH_METRICS[task_id]
    prediction = prediction.strip()

    # 修复：只有在原始预测为空时才判定为拒答，而非提取失败
    abstained = not prediction

    if task_id in {"1-2", "2-8", "3-6"}:
        expected = extract_gold_options(reference)
        actual = extract_option_set(prediction)
        return ItemScore(float(actual == expected and bool(expected)), metric, abstained, sorted(actual), sorted(expected), not actual and not abstained)
    if task_id in {"1-1", "2-7", "3-2", "3-8"}:
        cleaned = reference.removeprefix("答案:").removeprefix("答案：")
        return ItemScore(rouge_l(cleaned, prediction), metric, abstained, prediction or None, cleaned)
    if task_id == "2-1":
        origin = origin_sentence(question)
        return ItemScore(correction_f05(origin, reference, prediction), metric, abstained, prediction or None, reference)
    if task_id in {"2-2", "2-3", "2-4", "2-9", "3-3"}:
        expected = split_reference_labels(task_id, reference)
        ontology = set(label_space) or task_label_space(task_id)
        actual = parse_label_answer(task_id, prediction, ontology)
        score = float(actual == expected) if task_id in {"2-2", "2-4"} else set_f1(expected, actual)
        return ItemScore(score, metric, abstained, sorted(actual), sorted(expected), not actual and not abstained)
    if task_id == "2-5":
        cleaned = reference.removeprefix("回答:").removeprefix("回答：")
        return ItemScore(char_f1(cleaned, prediction), metric, abstained, prediction or None, cleaned)
    if task_id == "2-6":
        parsed = extract_entities(prediction.replace("**", ""), ENTITY_TYPES)
        return ItemScore(entity_f1(reference, prediction.replace("**", "")), metric, abstained, prediction or None, reference, not parsed and not abstained)
    if task_id == "2-10":
        expected = [part for part in re.split(r"[;；]", reference) if part]
        actual = [part for part in re.split(r"[;；]", prediction) if part]
        paired = [char_f1(left, right) for left, right in zip(expected, actual)]
        precision = sum(paired) / len(actual) if actual else 0.0
        recall = sum(paired) / len(expected) if expected else 0.0
        score = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return ItemScore(score, metric, abstained, actual or None, expected)
    if task_id == "3-1":
        expected = split_reference_labels(task_id, reference)
        actual = extract_articles(prediction)
        return ItemScore(set_f1(expected, actual), metric, abstained, sorted(actual), sorted(expected), not actual and not abstained)
    if task_id in {"3-4", "3-5"}:
        expected = extract_months(reference)
        actual = extract_final_months(prediction)
        if expected is None:
            # Upstream ljp_imprison.py skips death/life records as data
            # imperfections. Our all-question mean retains zero and flags them
            # separately; do not mislabel them as model refusal/parse failure.
            return ItemScore(0.0, metric, abstained, actual, expected, False, True)
        distance = math.log(216) if actual is None else abs(math.log(expected + 1) - math.log(actual + 1))
        score = max(0.0, (math.log(216) - distance) / math.log(216))
        return ItemScore(score, metric, abstained, actual, expected, actual is None and not abstained)
    if task_id == "3-7":
        expected_match = re.search(r"犯罪金额[:：]\s*(\d+(?:\.\d+)?)", reference)
        expected = float(expected_match.group(1)) if expected_match else None

        actual = extract_final_amount(prediction)
        return ItemScore(float(actual is not None and actual == expected), metric, abstained, actual, expected, actual is None and not abstained)
    raise ValueError(f"不支持的 LawBench 任务：{task_id}")


def score_lexeval_item(prediction: str, reference: str) -> ItemScore:
    expected = extract_gold_options(reference)
    actual = extract_option_set(prediction)
    return ItemScore(
        float(actual == expected and bool(expected)),
        "exact_set_accuracy",
        not actual,
        sorted(actual),
        sorted(expected),
    )


def score_prediction(
    task_id: str,
    prediction: str,
    reference: str,
    dataset: str = "lawbench"
) -> ItemScore:
    """
    统一评分入口函数

    Args:
        task_id: 任务标识符（如 "1-1", "2-1" 等）
        prediction: 模型预测文本
        reference: 标准答案文本
        dataset: 数据集名称，"lawbench" 或 "lexeval"

    Returns:
        ItemScore: 包含分数、指标名、是否拒答、解析结果的评分对象

    Raises:
        ValueError: 不支持的数据集或任务ID

    Example:
        >>> score_prediction("1-2", "答案是A", "A", "lawbench")
        ItemScore(score=1.0, metric='accuracy', abstained=False, ...)
    """
    dataset = dataset.lower().strip()

    if dataset == "lawbench":
        return score_lawbench_item(task_id, prediction, reference)
    elif dataset == "lexeval":
        return score_lexeval_item(prediction, reference)
    else:
        raise ValueError(f"不支持的数据集：{dataset}，仅支持 'lawbench' 或 'lexeval'")


def extract_crime_amount(text: str) -> float | None:
    """提取犯罪金额，处理千分位和单位"""
    # 移除千分位逗号
    text = text.replace(',', '').replace('，', '')

    # 匹配数字 + 单位
    match = re.search(r'([\d.]+)\s*(万|千)?元', text)
    if match:
        num = float(match.group(1))
        unit = match.group(2)
        if unit == '万':
            num *= 10000
        elif unit == '千':
            num *= 1000
        return num

    # 回退: 纯数字
    values = [float(x) for x in re.findall(r'\d+(?:\.\d+)?', text)]
    return values[0] if len(values) == 1 else None
