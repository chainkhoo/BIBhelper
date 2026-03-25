import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient


os.environ.setdefault("AIA_SKIP_VENV", "1")
os.environ.setdefault("AIA_AUTO_INSTALL", "0")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("BIBHELPER_DATA_ROOT", str(ROOT / ".test-service-data"))

import aia
from apps.service.app.main import ServiceConfig, create_app
from bib_core import GeneratedArtifact, RunResult


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


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_root = Path(self.tempdir.name)
        self.config = ServiceConfig(
            data_root=self.data_root,
            job_retention_days=7,
            max_upload_files=5,
            max_upload_bytes=50 * 1024 * 1024,
            max_concurrent_jobs=1,
            shortcut_api_token="token-123",
            web_admin_password="pass-123",
            session_secret="secret-123",
            templates_dir=ROOT / "apps/service/templates",
            static_dir=ROOT / "apps/service/static",
            enable_pdf=False,
        )
        self.app = create_app(config=self.config, processor=self.fake_processor)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    @staticmethod
    def fake_pdf_upload(name="sample.pdf"):
        return (name, b"%PDF-1.4\n%fake pdf\n", "application/pdf")

    @staticmethod
    def fake_processor(options):
        customer_dir = options.output_root / "Mary Jane"
        customer_dir.mkdir(parents=True, exist_ok=True)
        docx_path = customer_dir / "summary.docx"
        pdf_path = customer_dir / "summary.pdf"
        docx_path.write_text("ok", encoding="utf-8")
        pdf_path.write_bytes(b"%PDF-1.4\nartifact")
        return RunResult(
            job_id=None,
            classified={"savings": [path.name for path in options.input_files]},
            tasks=[{"type": "savings", "mode": "single", "files": [str(options.input_files[0])]}],
            artifacts=[
                GeneratedArtifact(
                    relative_path=str(docx_path.relative_to(options.output_root)),
                    kind="docx",
                    customer_name="Mary Jane",
                    plan_type="savings",
                    source_filenames=[path.name for path in options.input_files],
                ),
                GeneratedArtifact(
                    relative_path=str(pdf_path.relative_to(options.output_root)),
                    kind="pdf",
                    customer_name="Mary Jane",
                    plan_type="savings",
                    source_filenames=[path.name for path in options.input_files],
                ),
            ],
            warnings=[],
        )

    def login(self):
        response = self.client.post("/login", data={"password": "pass-123"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def test_api_requires_bearer_token(self):
        response = self.client.post(
            "/api/v1/process",
            files=[("files", self.fake_pdf_upload())],
        )
        self.assertEqual(response.status_code, 401)

    def test_api_process_returns_zip_and_job_metadata(self):
        response = self.client.post(
            "/api/v1/process",
            headers={"Authorization": "Bearer token-123"},
            files=[("files", self.fake_pdf_upload("proposal.pdf"))],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        job_id = response.headers["x-job-id"]

        status_response = self.client.get(
            f"/api/v1/jobs/{job_id}",
            headers={"Authorization": "Bearer token-123"},
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["status"], "completed")
        self.assertEqual(status_response.json()["result_filename"], "Mary Jane保险总结书.zip")

        download_response = self.client.get(
            f"/api/v1/jobs/{job_id}/download",
            headers={"Authorization": "Bearer token-123"},
        )
        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response.headers["content-type"], "application/zip")

    def test_web_requires_login_then_lists_history(self):
        response = self.client.get("/jobs", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

        self.login()
        upload_response = self.client.post(
            "/upload",
            files=[("files", self.fake_pdf_upload("history.pdf"))],
            follow_redirects=False,
        )
        self.assertEqual(upload_response.status_code, 303)
        detail_path = upload_response.headers["location"]

        detail_response = self.client.get(detail_path)
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn("completed", detail_response.text)

        jobs_response = self.client.get("/jobs")
        self.assertEqual(jobs_response.status_code, 200)
        self.assertIn("history.pdf", jobs_response.text)

    def test_api_rejects_invalid_pdf(self):
        response = self.client.post(
            "/api/v1/process",
            headers={"Authorization": "Bearer token-123"},
            files=[("files", ("bad.pdf", b"not-a-pdf", "application/pdf"))],
        )
        self.assertEqual(response.status_code, 400)

    def test_api_rejects_when_concurrency_limit_reached(self):
        acquired = self.app.state.job_semaphore.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            response = self.client.post(
                "/api/v1/process",
                headers={"Authorization": "Bearer token-123"},
                files=[("files", self.fake_pdf_upload("busy.pdf"))],
            )
            self.assertEqual(response.status_code, 429)
        finally:
            self.app.state.job_semaphore.release()

    def test_templates_page_lists_current_templates(self):
        self.login()
        response = self.client.get("/templates")
        self.assertEqual(response.status_code, 200)
        self.assertIn("模板管理", response.text)
        self.assertIn("储蓄险单独总结书模板", response.text)
        self.assertTrue(self.app.state.template_store.current_path("savings_single").exists())

    def test_template_upload_and_restore(self):
        self.login()
        template_store = self.app.state.template_store
        current_path = template_store.current_path("savings_single")
        original_bytes = current_path.read_bytes()

        upload_response = self.client.post(
            "/templates/savings_single/upload",
            files={
                "file": (
                    "template_savings_standalone.docx",
                    b"new-template-content",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            follow_redirects=False,
        )
        self.assertEqual(upload_response.status_code, 303)
        self.assertEqual(current_path.read_bytes(), b"new-template-content")

        template_data = template_store.get_template("savings_single")
        self.assertGreaterEqual(len(template_data["history"]), 1)
        version_name = template_data["history"][0]["name"]

        restore_response = self.client.post(
            "/templates/savings_single/restore",
            data={"version_name": version_name},
            follow_redirects=False,
        )
        self.assertEqual(restore_response.status_code, 303)
        self.assertEqual(current_path.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
