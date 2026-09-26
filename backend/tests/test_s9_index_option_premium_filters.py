import os
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.services.screener_engine import S9_FILTER_WEIGHTS  # noqa: E402


class TestS9EntryScoreWeights(unittest.TestCase):
    def test_s9_entry_weights_total_one_hundred(self) -> None:
        self.assertEqual(sum(S9_FILTER_WEIGHTS.values()), 100)
        self.assertEqual(S9_FILTER_WEIGHTS["master_trend"], 20)
        self.assertEqual(S9_FILTER_WEIGHTS["entry_trigger"], 20)


if __name__ == "__main__":
    unittest.main()
