import os
import sys
import unittest
from pathlib import Path
from unittest import mock


os.environ.setdefault("AIA_SKIP_VENV", "1")
os.environ.setdefault("AIA_AUTO_INSTALL", "0")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aia


class FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class FakePdf:
    def __init__(self, pages):
        self.pages = [FakePage(text) for text in pages]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class NameExtractionTests(unittest.TestCase):
    def test_extract_name_and_age_allows_spaces(self):
        text = "受保人姓名: Mary Jane 女士 年龄: 31"
        self.assertEqual(aia._extract_name_and_age_from_text(text), ("Mary Jane", 31))

    def test_extract_name_and_age_does_not_capture_label_text(self):
        text = "受保人姓名: Mary Jane\n年龄: 31"
        self.assertEqual(aia._extract_name_and_age_from_text(text), ("Mary Jane", 31))

    def test_extract_payment_term_and_age_preserves_space_name(self):
        fake_pdf = FakePdf([
            "受保人姓名: Mary Jane 女士 年龄: 31\n5年缴费"
        ])
        with mock.patch.object(aia.pdfplumber, "open", return_value=fake_pdf):
            with mock.patch.object(aia, "_decode_special_sequences", side_effect=lambda path, text, pdf=None: text):
                payment_term, age, name = aia.extract_payment_term_and_age("fake.pdf")

        self.assertEqual(payment_term, 5)
        self.assertEqual(age, 31)
        self.assertEqual(name, "Mary Jane")


class SavingsTaskTests(unittest.TestCase):
    def test_build_savings_tasks_creates_comparison_for_same_person(self):
        files = [
            str(ROOT / "sample_a.pdf"),
            str(ROOT / "sample_b.pdf"),
        ]
        metadata = {
            files[0]: {"name": "Mary Jane", "age": 31},
            files[1]: {"name": "Mary Jane", "age": 31},
        }

        tasks = aia._build_savings_tasks(files, metadata)

        self.assertIn(
            {
                "type": "savings",
                "mode": "comparison",
                "files": sorted(files),
            },
            tasks,
        )


class ScopeRegressionTests(unittest.TestCase):
    def test_current_scope_excludes_education(self):
        self.assertNotIn("education", aia.PLAN_CONFIG)
        self.assertEqual(set(aia.PARSE_FUNCTIONS), {"savings", "critical_illness"})
        self.assertEqual(aia.classify_by_payment_term_and_age(5, 10, "儿童方案.pdf"), "savings")
        self.assertIsNone(aia.classify_by_payment_term_and_age(None, 10, "教育金方案.pdf"))


if __name__ == "__main__":
    unittest.main()
