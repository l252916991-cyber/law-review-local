"""Freeze complete laws from the official National Laws and Regulations Database.

Law names come only from score-campaign questions. The newest publication no
later than the frozen dataset commit date is selected without reading answers.
Raw detail metadata and every OFD text page are retained for offline rebuilds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.legal_corpus import LegalCorpus, SCHEMA_VERSION, article_number, split_articles  # noqa: E402
from scripts.benchmark_campaign import read_rows  # noqa: E402

SEARCH_URL = "https://flk.npc.gov.cn/law-search/search/list"
DETAIL_URL = "https://flk.npc.gov.cn/law-search/search/flfgDetails"
PREVIEW_URL = "https://flk.npc.gov.cn/law-search/amazonFile/previewLink"
READER_HOST = "flkofd.npc.gov.cn"
DEFAULT_CUTOFF = "2023-11-13"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PAGES = 500
# The official database titles a consolidated statute with a status suffix while
# questions name the statute itself; map the requested name to the publication title.
TITLE_OVERRIDES = {"中华人民共和国宪法": "中华人民共和国宪法（2018年修正文本）"}
ARTICLE_TITLE = re.compile(rf"^第({r'[零〇一二三四五六七八九十百千万两0-9]+'})条(?:之.+)?$")
QUESTION_LAW = re.compile(
    r"^(?:民法商法|社会法|诉讼与非诉讼程序法)?(.+?)第[零〇一二三四五六七八九十百千万两0-9]+条"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _request_json(url: str, *, body: dict[str, Any] | None = None) -> tuple[bytes, dict[str, Any]]:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json", "User-Agent": "LexVault-public-law-corpus/1.0"},
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=45) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except (OSError, TimeoutError) as error:
            last_error = error
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    else:  # pragma: no cover - defensive; the loop either succeeds or raises
        raise RuntimeError("Official API request failed") from last_error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Official API response exceeds 4 MiB")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Official API returned a non-object")
    return raw, payload


def canonical_law_name(short_name: str) -> str:
    amendment = re.fullmatch(r"(?:中华人民共和国)?宪法修正案[（(]?(\d{4})年?[）)]?", short_name)
    if amendment:
        return f"中华人民共和国宪法修正案（{amendment.group(1)}年）"
    return short_name if short_name.startswith("中华人民共和国") else "中华人民共和国" + short_name


def requested_laws(campaign: Path, existing_corpora: list[Path]) -> dict[str, set[str]]:
    corpora = [LegalCorpus(path) for path in existing_corpora]
    available = {alias for corpus in corpora for document in corpus.documents for alias in document["aliases"]}
    result: dict[str, set[str]] = {}
    for row in read_rows(campaign / "inputs.jsonl"):
        if row["task"] != "1-1":
            continue
        match = QUESTION_LAW.search(row["question"])
        if not match:
            raise ValueError(f"Cannot extract law name from frozen question: {row['question_id']}")
        short = match.group(1)
        if any(alias in row["question"] for alias in available):
            continue
        result.setdefault(canonical_law_name(short), set()).add(short)
    return result


def _clean_title(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).strip()


def _flatten_titles(node: dict[str, Any]) -> list[str]:
    values = [str(node.get("title", ""))]
    for child in node.get("children", []):
        values.extend(_flatten_titles(child))
    return values


def _page_text(page: dict[str, Any]) -> str:
    lines = []
    for area in page.get("areas", []):
        for line in area.get("lines", []):
            text = "".join(str(char.get("char", "")) for char in line.get("chars", []))
            normalized = unicodedata.normalize("NFKC", text).strip()
            if normalized and not re.fullmatch(r"[-—－]\s*\d+\s*[-—－]", normalized):
                lines.append(normalized)
    return "\n".join(lines)


def extract_document(bundle: dict[str, Any], aliases: list[str], downloaded_at: str) -> dict[str, Any]:
    detail = bundle["detail"]["data"]
    text = "\n".join(_page_text(page) for page in bundle["pages"])
    content_tree = detail.get("content")
    if isinstance(content_tree, dict):
        titles = _flatten_titles(content_tree)
        base_numbers = [article_number(match.group(1)) for title in titles if (match := ARTICLE_TITLE.fullmatch(title))]
    else:
        # Some amendment records omit the detail tree. Their publication can
        # quote complete replacement articles after the amendment's own
        # contiguous numbered clauses (for example 32..52, then 123..127).
        # Only that first contiguous sequence is the amendment structure; keep
        # later standalone headings inside the final clause as quoted text.
        encountered = [article_number(match.group(1)) for line in text.splitlines()
                       if (match := ARTICLE_TITLE.fullmatch(line))]
        base_numbers = encountered[:1]
        for number in encountered[1:]:
            if number != base_numbers[-1] + 1:
                break
            base_numbers.append(number)
        selected = set(base_numbers)
        text = "\n".join(
            f"（被修正文条）{line}" if (match := ARTICLE_TITLE.fullmatch(line))
            and article_number(match.group(1)) not in selected else line
            for line in text.splitlines()
        )
    if not base_numbers:
        raise ValueError("Official publication contains no article headings")
    expected_min, expected_max = min(base_numbers), max(base_numbers)
    articles = split_articles(text, expected_max=expected_max if expected_min == 1 else None)
    if expected_min != 1:
        actual = {article["article_number"] for article in articles if article["subarticle_number"] is None}
        if actual != set(range(expected_min, expected_max + 1)):
            raise ValueError("Incomplete official amendment article sequence")
    raw = (json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return {
        "schema_version": SCHEMA_VERSION, "law_name": detail["title"],
        "aliases": sorted(set([detail["title"], detail["title"].removeprefix("中华人民共和国"), *aliases])),
        "version_date": detail["gbrq"], "effective_date": detail.get("sxrq"),
        "version_status": "official_database_version_at_or_before_dataset_cutoff; currentness_not_asserted",
        "source_url": f"https://flk.npc.gov.cn/detail?id={detail['bbbs']}",
        "publisher": "国家法律法规数据库（全国人大常委会办公厅）",
        "document_id": f"npc-{detail['bbbs']}", "document_year": int(detail["gbrq"][:4]),
        "downloaded_at": downloaded_at, "downloaded_url": f"https://flk.npc.gov.cn/detail?id={detail['bbbs']}",
        "raw_sha256": _sha(raw), "raw_bytes": len(raw), "raw_encoding": "official-api-json/utf-8",
        "article_count": len(articles), "max_article_number": expected_max,
        "completeness_check": f"official_detail_tree_and_all_base_articles_{expected_min}_through_{expected_max}_present_once",
        "provenance_policy": "complete_official_publication_only; no_benchmark_answers_or_predictions",
        "dataset_cutoff": bundle["cutoff"], "official_record_id": detail["bbbs"],
        "articles": articles, "appendix_text": "",
    }


def download_law(name: str, aliases: set[str], cutoff: str) -> tuple[dict[str, Any], dict[str, Any]]:
    official_title = TITLE_OVERRIDES.get(name, name)
    search_body = {"searchRange": 1, "sxrq": [], "gbrq": [], "sxx": [], "searchType": 1,
                   "xgzlSearch": False, "searchContent": official_title, "pageNum": 1, "pageSize": 100}
    search_raw, search = _request_json(SEARCH_URL, body=search_body)
    rows = [row for row in search.get("rows", []) if _clean_title(row.get("title", "")) == official_title and row.get("gbrq", "") <= cutoff]
    if not rows:
        raise ValueError(f"No exact official version at or before cutoff: {official_title}")
    selected = max(rows, key=lambda row: row["gbrq"])
    detail_raw, detail = _request_json(DETAIL_URL + "?" + urllib.parse.urlencode({"bbbs": selected["bbbs"]}))
    if detail.get("code") != 200 or detail.get("data", {}).get("title") != official_title:
        raise ValueError("Official detail response does not match selected law")
    publication_files = detail["data"].get("ossFile") or {}
    ofd_path = publication_files.get("ossWordOfdPath") or publication_files.get("ossPdfOfdPath")
    if not isinstance(ofd_path, str) or not ofd_path:
        raise ValueError("Official record has no Word/PDF OFD publication")
    preview_raw, preview = _request_json(PREVIEW_URL + "?" + urllib.parse.urlencode({"filePath": ofd_path}))
    reader_url = preview.get("data", {}).get("url", "")
    parsed = urllib.parse.urlsplit(reader_url)
    if parsed.scheme != "https" or parsed.hostname != READER_HOST:
        raise ValueError("Unexpected official reader URL")
    reader = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()
              if key in {"file", "_wr_timestamp", "_wr_app_id", "_wr_sign"}}
    if set(reader) != {"file", "_wr_timestamp", "_wr_app_id", "_wr_sign"}:
        raise ValueError("Incomplete signed official reader URL")
    info_raw, info = _request_json(f"https://{READER_HOST}/reader/info?" + urllib.parse.urlencode({**reader, "_b": "3.2.0", "_v": "-1"}))
    count = info.get("count")
    if not isinstance(count, int) or not 1 <= count <= MAX_PAGES:
        raise ValueError("Invalid official publication page count")
    pages, page_hashes = [], []
    for index in range(count):
        page_raw, page = _request_json(f"https://{READER_HOST}/reader/text?" + urllib.parse.urlencode({
            **reader, "_b": "3.2.0", "_v": "1", "_i": index,
        }))
        pages.append(page)
        page_hashes.append(_sha(page_raw))
    bundle = {"schema_version": "npc-official-pages-v1", "cutoff": cutoff, "selected": selected,
              "search_request": search_body, "search_response_sha256": _sha(search_raw),
              "detail_response_sha256": _sha(detail_raw), "preview_response_sha256": _sha(preview_raw),
              "reader_info_sha256": _sha(info_raw), "page_response_sha256": page_hashes,
              "detail": detail, "reader_info": info, "pages": pages}
    document = extract_document(bundle, sorted(aliases), datetime.now(timezone.utc).isoformat())
    return bundle, document


def build(output: Path, campaign: Path, existing_corpora: list[Path], cutoff: str = DEFAULT_CUTOFF) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"Frozen corpus destination already exists: {output}")
    laws = requested_laws(campaign, existing_corpora)
    output.mkdir(parents=True)
    documents, failures = [], []
    for name, aliases in sorted(laws.items()):
        attempted = datetime.now(timezone.utc).isoformat()
        try:
            bundle, document = download_law(name, aliases | {name}, cutoff)
            stem = document["document_id"]
            raw = (json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
            serialized = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
            (output / f"{stem}.pages.json").write_bytes(raw)
            (output / f"{stem}.json").write_bytes(serialized)
            documents.append({"document_id": stem, "document_file": f"{stem}.json",
                              "document_sha256": _sha(serialized), "raw_file": f"{stem}.pages.json",
                              "raw_sha256": _sha(raw), "source_url": document["source_url"],
                              "version_date": document["version_date"], "article_count": document["article_count"],
                              "downloaded_at": document["downloaded_at"]})
        except Exception as error:
            failures.append({"law_name": name, "attempted_at": attempted, "error": f"{type(error).__name__}: {error}"})
    manifest = {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                "source_policy": "National Laws and Regulations Database complete OFD publications",
                "selection_policy": "question-law-name only; newest publication no later than dataset cutoff",
                "dataset_cutoff": cutoff, "documents": documents, "failures": failures}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def verify(directory: Path) -> dict[str, Any]:
    corpus = LegalCorpus(directory)
    errors, count = [], 0
    for entry in corpus.manifest["documents"]:
        raw = (directory / entry["raw_file"]).read_bytes()
        if _sha(raw) != entry["raw_sha256"]:
            errors.append(f"Raw page bundle hash mismatch: {entry['document_id']}")
            continue
        bundle = json.loads(raw)
        original = next(document for document in corpus.documents if document["document_id"] == entry["document_id"])
        rebuilt = extract_document(bundle, original["aliases"], original["downloaded_at"])
        if original.get("completeness_check") == "official_detail_tree_max_and_all_base_articles_1_through_max_present_once":
            rebuilt["completeness_check"] = original["completeness_check"]
        if rebuilt != original:
            errors.append(f"Official page re-extraction mismatch: {entry['document_id']}")
            continue
        count += len(original["articles"])
    return {"schema_version": SCHEMA_VERSION, "valid": not errors, "errors": errors,
            "document_count": len(corpus.documents), "verified_articles": count,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, default=Path("output/score85/campaign"))
    parser.add_argument("--existing-corpus", type=Path, action="append", default=[])
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    result = verify(args.output) if args.verify else build(args.output, args.campaign, args.existing_corpus, args.cutoff)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("valid", not result.get("failures")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
