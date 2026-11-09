"""Vision transcription adapter: opt-in, bounded, and never a silent empty page."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.documents import vlm_ocr
from app.documents.ingest import extract_image_text


class _FakeResponse:
    def __init__(self, payload: dict):
        self.buffer = io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def read1(self, size: int) -> bytes:
        return self.buffer.read(size)

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class _FakeOpener:
    def __init__(self, payload: dict):
        self.payload = payload
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse(self.payload)


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


class VlmSettingsTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LAW_REVIEW_VLM_OCR", None)
            self.assertFalse(vlm_ocr.vlm_enabled())
            with self.assertRaises(vlm_ocr.VlmUnavailable):
                vlm_ocr.vlm_settings()

    def test_enabled_without_endpoint_is_unavailable(self):
        with patch.dict(os.environ, {"LAW_REVIEW_VLM_OCR": "on", "LAW_REVIEW_VLM_URL": ""}):
            with self.assertRaises(vlm_ocr.VlmUnavailable):
                vlm_ocr.vlm_settings()

    def test_remote_plain_http_endpoint_is_refused(self):
        env = {"LAW_REVIEW_VLM_OCR": "on", "LAW_REVIEW_VLM_URL": "http://example.com/v1", "LAW_REVIEW_VLM_MODEL": "vl"}
        with patch.dict(os.environ, env):
            with self.assertRaises(vlm_ocr.VlmUnavailable):
                vlm_ocr.vlm_settings()

    def test_status_never_raises(self):
        status = vlm_ocr.describe_status()
        self.assertFalse(status["available"])
        self.assertIn("reason", status)


class ImageToTextTests(unittest.TestCase):
    ENV = {"LAW_REVIEW_VLM_OCR": "on", "LAW_REVIEW_VLM_URL": "http://127.0.0.1:8100/v1", "LAW_REVIEW_VLM_MODEL": "vl"}

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-vlm-")
        self.addCleanup(self.directory.cleanup)
        self.image = Path(self.directory.name) / "page.png"
        self.image.write_bytes(b"\x89PNG fake")

    def _call(self, payload: dict) -> tuple[str, _FakeOpener]:
        opener = _FakeOpener(payload)
        with patch.dict(os.environ, self.ENV), patch.object(vlm_ocr, "egress_opener", return_value=opener):
            return vlm_ocr.image_to_text(self.image), opener

    def test_returns_transcription_and_sends_a_data_uri(self):
        text, opener = self._call(_reply("第一行\n第二行"))
        self.assertEqual(text, "第一行\n第二行")
        body = opener.requests[0]
        self.assertEqual(body["model"], "vl")
        image_part = body["messages"][0]["content"][1]
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_reasoning_block_is_stripped(self):
        text, _ = self._call(_reply("<think>scratch</think>正文"))
        self.assertEqual(text, "正文")

    def test_empty_content_is_an_error(self):
        with patch.dict(os.environ, self.ENV):
            with self.assertRaises(vlm_ocr.VlmError):
                with patch.object(vlm_ocr, "egress_opener", return_value=_FakeOpener(_reply(""))):
                    vlm_ocr.image_to_text(self.image)

    def test_oversized_image_is_rejected_before_upload(self):
        with patch.dict(os.environ, self.ENV), patch.object(vlm_ocr, "MAX_IMAGE_BYTES", 3):
            with self.assertRaises(vlm_ocr.VlmError):
                vlm_ocr.image_to_text(self.image)

    def test_transport_failure_is_wrapped(self):
        class Boom:
            def open(self, request, timeout=None):
                raise OSError("down")

        with patch.dict(os.environ, self.ENV), patch.object(vlm_ocr, "egress_opener", return_value=Boom()):
            with self.assertRaises(vlm_ocr.VlmError):
                vlm_ocr.image_to_text(self.image)


class OcrFallbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-ocr-")
        self.addCleanup(self.directory.cleanup)
        self.image = Path(self.directory.name) / "page.png"
        self.image.write_bytes(b"\x89PNG fake")

    def test_vision_text_is_preferred_when_available(self):
        with patch("app.documents.vlm_ocr.image_to_text", return_value="视觉转录"), \
                patch("app.documents.ingest.run_text_command") as tesseract:
            self.assertEqual(extract_image_text(self.image), ["视觉转录"])
        tesseract.assert_not_called()

    def test_tesseract_is_used_when_vision_fails(self):
        with patch("app.documents.vlm_ocr.image_to_text", side_effect=vlm_ocr.VlmUnavailable("off")), \
                patch("app.documents.ingest.run_text_command", return_value="OCR 文本") as tesseract:
            self.assertEqual(extract_image_text(self.image), ["OCR 文本"])
        self.assertEqual(tesseract.call_count, 1)

    def test_empty_vision_result_falls_back_instead_of_returning_blank(self):
        with patch("app.documents.vlm_ocr.image_to_text", return_value="   "), \
                patch("app.documents.ingest.run_text_command", return_value="OCR 兜底"):
            self.assertEqual(extract_image_text(self.image), ["OCR 兜底"])


if __name__ == "__main__":
    unittest.main()
