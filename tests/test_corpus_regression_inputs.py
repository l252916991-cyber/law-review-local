from __future__ import annotations

import hashlib
import json

import pytest


def frozen_input(root):
    directory = root / "source"
    directory.mkdir()
    raw = b"public publication"
    document = b'{"schema_version":"lexvault-public-laws-v1","articles":[]}'
    (directory / "source.html").write_bytes(raw)
    (directory / "document.json").write_bytes(document)
    manifest = json.dumps({"schema_version": "lexvault-public-laws-v1", "documents": [{
        "document_file": "document.json", "document_sha256": hashlib.sha256(document).hexdigest(),
        "raw_file": "source.html", "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }]}).encode()
    (directory / "manifest.json").write_bytes(manifest)
    return {"sources": [{"path": "source", "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                         "document_count": 1}], "expected": {"publication_count": 1}}


def test_frozen_inputs_load_only_the_registered_sources(tmp_path, corpus_regression_loader):
    contract = frozen_input(tmp_path)
    (tmp_path / "unregistered").mkdir()
    result = corpus_regression_loader(tmp_path, contract)
    assert result["directories"] == [str((tmp_path / "source").resolve())]
    assert result["expected"]["publication_count"] == 1


@pytest.mark.parametrize(("filename", "reason"), [
    ("manifest.json", "manifest identity"),
    ("document.json", "integrity mismatch"),
    ("source.html", "raw publication digest"),
])
def test_changed_frozen_bytes_fail_before_asset_regression(tmp_path, corpus_regression_loader, filename, reason):
    contract = frozen_input(tmp_path)
    (tmp_path / "source" / filename).write_bytes(b"changed")
    with pytest.raises(ValueError, match=reason):
        corpus_regression_loader(tmp_path, contract)


def test_missing_or_escaping_asset_path_never_becomes_a_skip(tmp_path, corpus_regression_loader):
    contract = frozen_input(tmp_path)
    contract["sources"][0]["path"] = "../outside"
    with pytest.raises(ValueError, match="escapes asset root"):
        corpus_regression_loader(tmp_path, contract)
    contract["sources"][0]["path"] = "missing"
    with pytest.raises(FileNotFoundError):
        corpus_regression_loader(tmp_path, contract)
