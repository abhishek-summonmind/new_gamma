import os
import sys
import unittest
from datetime import date
from pathlib import Path

from sqlalchemy import delete

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings
from app.db import SessionLocal, init_db
from app.models import DhanConfig
from app.services.dhan_config_service import DhanConfigService


class TestDhanConfigService(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        self.settings = Settings(database_url="sqlite:///./test.db")
        self.service = DhanConfigService(settings=self.settings)
        os.environ.pop("OPTION_EXPIRY", None)
        os.environ.pop("GROWW_OPTION_EXPIRY", None)
        with SessionLocal() as db:
            db.execute(delete(DhanConfig))
            db.commit()

    def test_database_config_takes_precedence_over_environment(self) -> None:
        os.environ["DHAN_ACCESS_TOKEN"] = "env-token"
        os.environ["DHAN_OPTION_EXPIRY"] = "2026-09-01"

        self.service.set_access_token("db-token")
        self.service.set_option_expiry("2026-10-01")

        self.assertEqual(self.service.get_access_token(), "db-token")
        self.assertEqual(self.service.get_option_expiry().isoformat(), "2026-10-01")

        status = self.service.get_config_status()
        self.assertEqual(status["access_token"]["source"], "database")
        self.assertEqual(status["option_expiry"]["source"], "database")
        self.assertEqual(status["option_expiry"]["value"], "2026-10-01")

    def test_clear_config_reverts_to_environment(self) -> None:
        os.environ["DHAN_ACCESS_TOKEN"] = "env-token"
        os.environ["DHAN_OPTION_EXPIRY"] = "2026-09-01"

        self.service.set_access_token("db-token")
        self.service.set_option_expiry("2026-10-01")
        self.service.clear_config()

        self.assertEqual(self.service.get_access_token(), "env-token")
        self.assertEqual(self.service.get_option_expiry().isoformat(), "2026-09-01")

        status = self.service.get_config_status()
        self.assertEqual(status["access_token"]["source"], "environment")
        self.assertEqual(status["option_expiry"]["source"], "environment")
        self.assertEqual(status["option_expiry"]["value"], "2026-09-01")

    def test_generic_option_expiry_environment_takes_precedence_over_legacy_dhan_env(self) -> None:
        os.environ["OPTION_EXPIRY"] = "2026-11-01"
        os.environ["DHAN_OPTION_EXPIRY"] = "2026-09-01"

        self.assertEqual(self.service.get_option_expiry().isoformat(), "2026-11-01")

        status = self.service.get_config_status()
        self.assertEqual(status["option_expiry"]["source"], "environment")
        self.assertEqual(status["option_expiry"]["value"], "2026-11-01")

    def test_set_access_token_rejects_empty_string(self) -> None:
        with self.assertRaises(ValueError):
            self.service.set_access_token("")

    def test_set_option_expiry_rejects_invalid_format(self) -> None:
        with self.assertRaises(ValueError):
            self.service.set_option_expiry("not-a-date")


if __name__ == "__main__":
    unittest.main()

