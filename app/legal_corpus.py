"""Versioned, source-verifiable public statutes; independent of benchmark labels.

The corpus is an explicit opt-in artifact. This module does not access application
databases, model outputs, or benchmark answers. An unspecified law version is
never silently resolved when several versions are present.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "lexvault-public-laws-v1"
ARTICLE_RE = re.compile(r"^第([零〇一二三四五六七八九十百千万两0-9]+)条(?:之([一二三四五六七八九十0-9]+))?(?:\s|[【〔（(]|$)")
HEADING_RE = re.compile(r"^(?:第[零〇一二三四五六七八九十百千万0-9]+[编章节](?:\s|$)|附\s*则$)")


class VersionAmbiguityError(ValueError):
    """The caller must name a version instead of blending different statutes."""


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def html_to_text(page: str) -> str:
    parser = _PageText()
    parser.feed(page)
    return "\n".join(
        line for raw in "".join(parser.parts).splitlines()
        if (line := re.sub(r"[\s\u3000]+", " ", raw).strip())
    )


def article_number(value: str) -> int:
    """Parse Chinese statute numbers without any model or reference answer."""
    value = unicodedata.normalize("NFKC", value)
    if value.isdigit():
        return int(value)
    digits = dict(zip("零〇一二三四五六七八九两", (0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2)))
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in units:
            total += (current or 1) * units[char]
            current = 0
        else:
            raise ValueError(f"Invalid article number: {value}")
    return total + current


def split_articles(text: str, expected_max: int | None = None) -> list[dict[str, Any]]:
    """Split paragraph-aligned statutory text; preserve paragraphs and subarticles.

    References inside a sentence are not new article headings. Chapter headings
    delimit the preceding article and are kept separately. HTML footer removal
    is the builder's explicit source-specific responsibility.
    """
    articles: list[dict[str, Any]] = []
    paragraphs: list[str] = []
    current: dict[str, Any] | None = None
    headings: list[str] = []

    def flush() -> None:
        if current is not None:
            current["text"] = "\n".join(paragraphs).strip()
            current["text_sha256"] = hashlib.sha256(current["text"].encode()).hexdigest()
            articles.append(current.copy())

    for raw in text.splitlines():
        line = re.sub(r"[\s\u3000]+", " ", raw).strip()
        if not line:
            continue
        match = ARTICLE_RE.match(line)
        if match:
            flush()
            number = article_number(match.group(1))
            sub = article_number(match.group(2)) if match.group(2) else None
            current = {
                "article_number": number,
                "subarticle_number": sub,
                "article_id": f"{number}-{sub}" if sub else str(number),
                "heading": match.group(0).strip(),
                "section_heading": headings[-1] if headings else None,
            }
            paragraphs = [line]
        elif HEADING_RE.match(line):
            flush()
            current = None
            paragraphs = []
            headings.append(line)
        elif current is not None:
            paragraphs.append(line)
    flush()
    ids = [item["article_id"] for item in articles]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate article headings: source selection is ambiguous")
    if expected_max is not None:
        actual = {item["article_number"] for item in articles if item["subarticle_number"] is None}
        expected = set(range(1, expected_max + 1))
        if actual != expected:
            raise ValueError(f"Incomplete statute: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")
    return articles


def _terms(text: str) -> Counter[str]:
    compact = "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text.lower()))
    return Counter(compact[i:i + 2] for i in range(len(compact) - 1))


class LegalCorpus:
    """Read-only retrieval with explicit version selection and artifact integrity."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        if self.manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported public law corpus schema")
        self.documents: list[dict[str, Any]] = []
        for entry in self.manifest["documents"]:
            path = self.directory / entry["document_file"]
            if path.resolve().parent != self.directory.resolve():
                raise ValueError("Document path must remain within the corpus directory")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["document_sha256"]:
                raise ValueError(f"Statute integrity mismatch: {entry['document_file']}")
            document = json.loads(raw)
            if document.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("Unsupported statute schema")
            self.documents.append(document)

    def versions(self, law_name: str) -> list[str | None]:
        return [item["version_date"] for item in self.documents if law_name in item["aliases"]]

    def _select(self, law_name: str | None, version_date: str | None) -> list[dict[str, Any]]:
        docs = [item for item in self.documents if law_name is None or law_name in item["aliases"]]
        if version_date is not None:
            docs = [item for item in docs if item["version_date"] == version_date]
        elif len({item["version_date"] for item in docs}) > 1 and law_name is not None:
            raise VersionAmbiguityError(f"Specify a version for {law_name}: {self.versions(law_name)}")
        return docs

    def lookup(self, law_name: str, number: int, *, version_date: str | None = None,
               subarticle: int | None = None) -> list[dict[str, Any]]:
        return [self._result(doc, article) for doc in self._select(law_name, version_date)
                for article in doc["articles"]
                if article["article_number"] == number and article["subarticle_number"] == subarticle]

    @staticmethod
    def _result(doc: dict[str, Any], article: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "law_name": doc["law_name"], "version_date": doc["version_date"],
            "effective_date": doc["effective_date"], "version_status": doc["version_status"],
            "source_url": doc["source_url"], "document_id": doc["document_id"],
            **article,
        }

    def search(self, query: str, *, law_name: str | None = None, version_date: str | None = None,
               limit: int = 5) -> list[dict[str, Any]]:
        """Chinese bigram TF-IDF; result labels always include version and source."""
        if limit < 1:
            return []
        candidates = [(doc, article) for doc in self._select(law_name, version_date) for article in doc["articles"]]
        vectors = [_terms(article["text"]) for _, article in candidates]
        query_terms = _terms(query)
        df = Counter(term for vector in vectors for term in vector)
        scores: list[tuple[float, int]] = []
        for index, vector in enumerate(vectors):
            score = sum((1 + math.log(vector[term])) * (math.log((len(vectors) + 1) / (df[term] + 1)) + 1)
                        for term in query_terms if vector[term]) / math.sqrt(sum(vector.values()) or 1)
            if score > 0:
                scores.append((score, index))
        return [{**self._result(*candidates[index]), "retrieval_score": round(score, 6)}
                for score, index in sorted(scores, key=lambda pair: (-pair[0], pair[1]))[:limit]]
