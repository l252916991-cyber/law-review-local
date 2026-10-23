"""Short-token escape hatch stays loopback-only and off by default."""
import json
import os
import unittest
from unittest.mock import patch

from app.security import AccessConfigurationError, configured_tokens

SHORT = "111"


class LocalTokenEscapeTests(unittest.TestCase):
    def test_short_token_rejected_by_default(self):
        env = {"LAW_REVIEW_API_TOKENS_JSON": json.dumps({SHORT: {"name": "管理员", "admin": True}})}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("LAW_REVIEW_ALLOW_INSECURE_LOCAL_TOKEN", None)
            with self.assertRaises(AccessConfigurationError):
                configured_tokens()

    def test_short_token_allowed_on_loopback_opt_in(self):
        env = {
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({SHORT: {"name": "管理员", "admin": True}}),
            "LAW_REVIEW_ALLOW_INSECURE_LOCAL_TOKEN": "1",
            "LAW_REVIEW_ALLOWED_HOSTS": "localhost,127.0.0.1,::1",
        }
        with patch.dict(os.environ, env, clear=False):
            tokens = configured_tokens()
        self.assertTrue(tokens[SHORT].admin)

    def test_short_token_refused_when_host_is_reachable(self):
        # The opt-in must not silently weaken a network-reachable deployment.
        env = {
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({SHORT: {"name": "管理员", "admin": True}}),
            "LAW_REVIEW_ALLOW_INSECURE_LOCAL_TOKEN": "1",
            "LAW_REVIEW_ALLOWED_HOSTS": "review.example.com",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(AccessConfigurationError):
                configured_tokens()


if __name__ == "__main__":
    unittest.main()
