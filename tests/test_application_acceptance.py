"""The acceptance probe is isolated and never calls models without --live."""
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from scripts.validate_application_acceptance import loopback_endpoint, main


def test_subprocess_mcp_probe_isolated_and_offline_by_default():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "report.json"
        with patch.dict(os.environ, {"LAW_REVIEW_DATA_DIR": directory}), \
                patch("scripts.validate_application_acceptance.live_models", side_effect=AssertionError("no live opt-in")):
            assert main(["--output", str(output)]) == 0
            assert os.environ["LAW_REVIEW_DATA_DIR"] == directory
        report = json.loads(output.read_text())
        assert report["mcp"]["ok"]
        assert not report["mcp"]["desktop_client_verified"]
        assert report["models"] == {"executed": False}
        assert not (Path(directory) / "law_review.db").exists()


@pytest.mark.parametrize("endpoint", ["https://example.com/v1", "http://user:secret@127.0.0.1/v1", "http://127.0.0.1/v1?key=secret"])
def test_acceptance_refuses_remote_or_credential_bearing_endpoints(endpoint):
    with pytest.raises(ValueError):
        loopback_endpoint(endpoint)


def test_live_route_without_separate_call_ledger_cannot_be_claimed_complete():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "report.json"
        with patch("app.tool_loop.run_tool_loop", return_value={"tool_calls": 1}), \
                patch("app.services.chat", return_value={"llm_used": True,
                      "provenance": {"route": {"fallback": True, "model": "synthetic-fallback"}}}):
            assert main(["--live", "--output", str(output)]) == 1
        report = json.loads(output.read_text())
        assert report["models"]["chat"]["model_route"]["fallback"] is True
        assert not report["models"]["complete"]
