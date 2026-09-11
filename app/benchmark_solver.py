"""Gold-blind, fully recorded local-model strategies for LawBench experiments.

This module intentionally imports neither the dataset nor the scorer. Its only
task-specific input is the public instruction and question supplied by callers.
Each strategy has a fixed call policy: direct/guided call once, verify calls
twice after a successful first call. Failed calls are never silently retried.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.services import LOCAL_LLM_MODEL, LOCAL_LLM_URL, read_json_with_deadline


SOLVER_VERSION = "lawbench-solver-v1"
DIRECT_SYSTEM = "你正在参加中国法律能力评测。严格遵循题目的任务说明和输出格式，直接给出所要求的答案，不展示思维过程，不添加题目未要求的分析或开场白。"

# General task methods, not examples, labels mined from answers, or legal facts.
# The question's own instruction always determines the final output format.
TASK_GUIDANCE = {
    "1-1": "先确定法律名称、条号及题目给定的版本；回忆完整条文的条件、行为和法律后果。按条文原有次序完整作答，保留款项、数值、例外和但书，不用概括代替原文，不自行增加版本说明。",
    "1-2": "逐项核对选项的适用主体、构成条件、程序及例外，留意题目问正确还是错误、单选还是多选。只输出要求的选项，不把分析中出现的选项写进最终答案。",
    "2-1": "逐字检查法律文书中的错别字、漏字、多字和明显错误用词。只纠正能够确定的文字错误，保留未出错的原句、原有事实、数值、标点和排版；不要润色、重写或增加说明。",
    "2-2": "先区分双方一致的背景事实与真正争执的问题，找出决定本案处理结果的核心争议。若任务给定候选焦点，只选对应的原始标签，不改写标签，不把所有背景问题并列为焦点。",
    "2-3": "分别检查婚姻关系、财产、债务、子女及损害赔偿等事实涉及的请求。仅从题目提供的类别中选择确有事实支持的全部类别，保留原始类别名称；不要因一般关联加入没有出现的类别。",
    "2-4": "根据咨询或事实的主要法律关系确定主题，而不是凭孤立词语分类。若给定候选类别，比较最相近类别的区别后只输出所要求的类别原名。",
    "2-5": "先定位问题询问的人、时间、金额、行为或法律关系，再回到给定材料找直接依据。答案使用材料支持的最小完整片段；多人、多笔或多次行为要对应准确，不能用外部常识补造事实。",
    "2-6": "按任务定义逐一找出原文中的实体及其类型，保留原文实体的完整边界、姓名、金额和单位。实体必须实际出现在文本中；同一实体不要因解释而扩写，严格遵循任务要求的字段和分隔格式。",
    "2-7": "概括事件主体、核心行为、结果及题目要求的后续处置，保留关键事实和因果关系。删去重复、枝节、套话和个人评论；遵守摘要长度要求，不添加原文未披露的事实或法律结论。",
    "2-8": "分别判断候选句是在提出观点、支持观点还是仅陈述背景，再核对题目要求的论点关系和方向。按提供的标签或选项作答，不将关键词相似直接当成论证关系。",
    "2-9": "逐句检查真实发生的法律事件及其行为主体，排除否定、假设和仅提及但未发生的行为。若给定事件类别，只输出有证据支持的全部原始标签，避免遗漏并列事件或重复输出。",
    "2-10": "先按任务定义识别事件，再从原文复制直接表达该事件发生的最小触发词。不要把主体、客体、时间或整个句子当成触发词；不得生成原文没有的同义词，遵循原始输出结构。",
    "3-1": "将事实中的主体、客观行为、结果、主观状态和特殊情节对应到所适用的实体法条。注意特别规定、未遂、共犯等题目所问范围；输出题目所需的条号，不加入只是背景相关的条文。",
    "3-2": "先明确要解决的法律问题和法律关系，再选择直接规范该问题的条文，核对构成要件、法律后果、限制和例外。按题目要求给出法律名称、条号及有关内容。参考答案的写法是先用「根据《法律名称》第X条，」引出法条，再结合本场景简要概括该条的关键要件与法律后果，最后落到本场景的结论；不要照抄整条法条原文，不要罗列无关款项，不要加小标题或列表符号，避免泛泛讨论或罗列无关条文。",
    "3-3": "对照主体、主观故意或过失、行为方式、对象、结果和数额等构成要件，辨别近似罪名和罪数。只列有事实支持的法定罪名，保留完整规范名称，不能把一个含顿号的复合罪名拆成多个罪名。",
    "3-4": "先识别犯罪行为与量刑情节，再综合数额、次数、损害、未遂、自首、坦白、赔偿、谅解及前科等已给事实估计刑期。不要把罚金、羁押天数或缓刑考验期当成主刑刑期；若要求月数，将年换算成月，输出一个月数。",
    "3-5": "先依据题目明确给定的法条确定量刑范围，再结合案情中的从轻、从重、减轻及数罪等情节估计主刑。不要另换罪名或混入罚金和缓刑考验期；按要求将年数准确换算成月数，输出一个月数。",
    "3-6": "先分清各当事人、各行为的时间顺序与法律关系，再逐个核对选项的成立条件和例外。留意否定提问及单选多选要求；最终只输出所要求的选项，避免分析文字造成歧义。",
    "3-7": "先确定问题所问的犯罪金额口径，逐笔列出应计入的金额并统一元、万元等单位。避免重复累计总额与分项、混算本金与利息或把追回、退赔自动当成犯罪金额扣减；完成加减乘除并复核后只输出要求的金额及单位。",
    "3-8": "先直接回答当事人的具体问题，再说明适用规则、关键条件、例外和可执行的处理办法。只根据题目事实分析，对缺失事实使用条件表述；覆盖咨询的全部子问题，避免通用开场白、重复结论和无关内容。",
}

GUIDED_SYSTEM_SUFFIX = (
    "\n作答前在内部核对任务类型、关键事实与输出格式。以下方法只辅助理解；"
    "原始任务说明优先，不得增加其未要求的分析、标签、声明或格式。最终只提交答案。"
)
VERIFY_INSTRUCTION = (
    "请独立复核刚才的初稿。初稿可能错误，不能作为事实或标准答案。"
    "重新依据原始任务说明和题目核对结论、遗漏、数值单位及输出格式；"
    "保留正确部分，修正确定的错误，不为显示修改而改写已正确内容。"
    "直接输出完整最终答案，不输出审查说明、思维过程或修改对照。"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A local model response may not redirect question data off the machine."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Model URL must be an HTTP(S) loopback endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Model URL cannot contain credentials, query or fragment")
    host = parsed.hostname
    # Pin localhost to its numeric loopback address, without trusting DNS or a proxy.
    host = "127.0.0.1" if host.lower() == "localhost" else host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("Only numeric loopback addresses or localhost are allowed") from exc
    if not address.is_loopback or "%" in host:
        raise ValueError("Only loopback model endpoints are allowed")
    authority = f"[{host}]" if address.version == 6 else host
    if parsed.port is not None:
        authority += f":{parsed.port}"
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urllib.parse.urlunsplit((parsed.scheme, authority, path, "", ""))


def _configuration(config: dict[str, Any]) -> dict[str, Any]:
    # An explicit allowlist ensures unrelated caller metadata never enters a prompt.
    result = {
        "url": config.get("url", LOCAL_LLM_URL).rstrip("/"),
        "model": config.get("model", LOCAL_LLM_MODEL),
        "temperature": config.get("temperature", 0.0),
        "max_tokens": config.get("max_tokens", 900),
        "timeout": config.get("timeout", 180),
        "enable_thinking": config.get("enable_thinking", False),
        "strategy": config.get("strategy", "direct"),
    }
    _endpoint(result["url"])
    if not isinstance(result["model"], str) or not result["model"].strip():
        raise ValueError("model must be a nonempty string")
    if result["strategy"] not in {"direct", "task_guided", "verify"}:
        raise ValueError("Unknown solver strategy")
    for key in ("temperature", "timeout"):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
    if not 0 <= result["temperature"] <= 2 or result["timeout"] <= 0:
        raise ValueError("temperature must be in [0, 2] and timeout must be positive")
    if isinstance(result["max_tokens"], bool) or not isinstance(result["max_tokens"], int) or result["max_tokens"] < 1:
        raise ValueError("max_tokens must be a positive integer")
    if not isinstance(result["enable_thinking"], bool):
        raise ValueError("enable_thinking must be boolean")
    return result


def _final_content(content: Any, reasoning: Any) -> tuple[str, int]:
    """Retain final content only; reasoning-only or unfinished thinking is failure."""
    if content is None:
        content = ""
    if not isinstance(content, str) or (reasoning is not None and not isinstance(reasoning, str)):
        raise ValueError("Malformed model message content")
    reasoning_count = len(reasoning or "")
    closed = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
    for match in closed.finditer(content):
        reasoning_count += len(match.group(0)) - len("<think></think>")
    final = closed.sub("", content)
    if re.search(r"</?think\b", final, re.IGNORECASE):
        raise ValueError("Incomplete or malformed thinking block; no final answer accepted")
    return final, reasoning_count


def _call(messages: list[dict[str, str]], config: dict[str, Any], phase: str) -> dict[str, Any]:
    body = {
        "model": config["model"], "messages": messages,
        "temperature": config["temperature"], "max_tokens": config["max_tokens"],
        "stream": False, "chat_template_kwargs": {"enable_thinking": config["enable_thinking"]},
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    record: dict[str, Any] = {
        "phase": phase, "request_url": _endpoint(config["url"]), "request": body,
        "request_sha256": hashlib.sha256(encoded).hexdigest(), "latency_ms": 0.0,
        "raw_final_answer": "", "prediction": "", "reasoning_characters": 0,
        "finish_reason": None, "usage": None, "response_model": None, "error": None,
    }
    started = time.monotonic()
    try:
        request = urllib.request.Request(
            record["request_url"], data=encoded,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=config["timeout"]) as response:
            payload = read_json_with_deadline(response, config["timeout"], deadline=started + config["timeout"])
        choice = payload["choices"][0]
        record.update(finish_reason=choice.get("finish_reason"), usage=payload.get("usage"), response_model=payload.get("model"))
        message = choice["message"]
        final, reasoning_count = _final_content(message.get("content"), message.get("reasoning_content"))
        record.update(raw_final_answer=final, prediction=final.strip(), reasoning_characters=reasoning_count)
        if record["finish_reason"] == "length":
            raise ValueError("truncated_response: model exhausted max_tokens")
        if record["finish_reason"] != "stop":
            raise ValueError(f"Unexpected finish_reason: {record['finish_reason']}")
        if not final.strip():
            raise ValueError("empty_final_answer: reasoning is not a final answer")
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        record["latency_ms"] = (time.monotonic() - started) * 1000
    return record


def solve(task_id: str, instruction: str, question: str, config: dict[str, Any]) -> dict[str, Any]:
    """Run a fixed inference strategy, returning every request and its outcome.

    Defaults: direct, temperature=0, max_tokens=900, timeout=180 seconds per
    call, thinking=False, and the project's local URL/model. No network retry,
    scorer access, answer-based selection, or fallback to an earlier draft.
    Unknown config fields are ignored and never sent to the model.
    """
    started = time.monotonic()
    result: dict[str, Any] = {
        "prediction": "", "error": None, "latency_ms": 0.0, "finish_reason": None,
        "usage": None, "calls": [], "model_config": {}, "solver_version": SOLVER_VERSION,
    }
    try:
        effective = _configuration(config)
        result["model_config"] = effective
        if task_id not in TASK_GUIDANCE:
            raise ValueError(f"Unknown LawBench task: {task_id}")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction must be nonempty")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be nonempty")
        system = DIRECT_SYSTEM
        if effective["strategy"] != "direct":
            system += GUIDED_SYSTEM_SUFFIX + "\n本任务核对方法：" + TASK_GUIDANCE[task_id]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{instruction.strip()}\n{question}"},
        ]
        first = _call(messages, effective, "draft")
        result["calls"].append(first)
        selected = first
        if not first["error"] and effective["strategy"] == "verify":
            review_messages = messages + [
                {"role": "assistant", "content": first["prediction"]},
                {"role": "user", "content": VERIFY_INSTRUCTION},
            ]
            selected = _call(review_messages, effective, "verify")
            result["calls"].append(selected)
        result.update(
            prediction=selected["prediction"], error=selected["error"],
            finish_reason=selected["finish_reason"], usage=selected["usage"],
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["latency_ms"] = (time.monotonic() - started) * 1000
    return result
