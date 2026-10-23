"""Class-scoped database isolation for unittest AND pytest discovery."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


class IsolatedDatabaseTestCase(unittest.TestCase):
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        original_setup = cls.__dict__.get("setUpClass")
        original_teardown = cls.__dict__.get("tearDownClass")

        @classmethod
        def setup(current):
            directory = tempfile.TemporaryDirectory(prefix="lexvault-isolated-")
            current.addClassCleanup(directory.cleanup)
            environment = patch.dict(os.environ, {
                "LAW_REVIEW_DATA_DIR": directory.name,
                "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": directory.name + "/checkpoints.sqlite",
                "LAW_REVIEW_AUTH_MODE": "local",
                "LAW_REVIEW_ALLOWED_HOSTS": "localhost,127.0.0.1,::1,testserver",
            })
            environment.start()
            current.addClassCleanup(environment.stop)
            module = sys.modules[current.__module__]
            if hasattr(module, "TEST_DATA"):
                replaced = patch.object(module, "TEST_DATA", directory.name)
                replaced.start()
                current.addClassCleanup(replaced.stop)
            from app.db import init_db
            init_db(seed=False)
            if original_setup:
                original_setup.__func__(current)

        @classmethod
        def teardown(current):
            if original_teardown:
                original_teardown.__func__(current)

        cls.setUpClass = setup
        cls.tearDownClass = teardown
