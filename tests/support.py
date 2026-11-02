"""Class-scoped database isolation for unittest AND pytest discovery."""
import os
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

POSTGRES_APP_ROLE = "lexvault_app"


class IsolatedDatabaseTestCase(unittest.TestCase):
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        original_setup = cls.__dict__.get("setUpClass")
        original_teardown = cls.__dict__.get("tearDownClass")

        @classmethod
        def setup(current):
            directory = tempfile.TemporaryDirectory(prefix="lexvault-isolated-")
            current.addClassCleanup(directory.cleanup)
            values = {
                "LAW_REVIEW_DATA_DIR": directory.name,
                "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": directory.name + "/checkpoints.sqlite",
                "LAW_REVIEW_AUTH_MODE": "local",
                "LAW_REVIEW_ALLOWED_HOSTS": "localhost,127.0.0.1,::1,testserver",
            }
            # LAW_REVIEW_TEST_POSTGRES_URL: run this class against its own,
            # freshly created PostgreSQL database instead of a SQLite file.
            admin_url = os.environ.get("LAW_REVIEW_TEST_POSTGRES_URL")
            sqlite_only = getattr(current, "sqlite_only", "") or getattr(sys.modules[current.__module__], "SQLITE_ONLY", "")
            if sqlite_only:
                values["LAW_REVIEW_DATABASE_URL"] = ""  # an empty URL means the SQLite file
            elif admin_url:
                from app.db_postgres import create_database, drop_database, ensure_role

                # The application role owns the database and is not a superuser,
                # so row-level security (information walls) is in force.
                ensure_role(admin_url, POSTGRES_APP_ROLE)
                name = f"lexvault_t_{os.getpid()}_{uuid.uuid4().hex[:12]}"
                values["LAW_REVIEW_DATABASE_URL"] = create_database(admin_url, name, owner=POSTGRES_APP_ROLE)
                current.addClassCleanup(drop_database, admin_url, name)
            environment = patch.dict(os.environ, values)
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
