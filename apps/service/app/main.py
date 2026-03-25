from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Callable

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_SRC = REPO_ROOT / "packages" / "bib_core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from bib_core import GeneratedArtifact, PipelineError, RunOptions, RunResult, run_pipeline

try:
    import mammoth
except ImportError:  # pragma: no cover - handled via metadata status
    mammoth = None

SERVICE_TIMEZONE = timezone(timedelta(hours=8), name="GMT+8")


def utc_now():
    return datetime.now(SERVICE_TIMEZONE)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(SERVICE_TIMEZONE).replace(microsecond=0).isoformat()


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SERVICE_TIMEZONE)
    return parsed


@dataclass
class ServiceConfig:
    data_root: Path
    job_retention_days: int
    max_upload_files: int
    max_upload_bytes: int
    max_concurrent_jobs: int
    shortcut_api_token: str
    web_admin_password: str
    session_secret: str
    templates_dir: Path
    static_dir: Path
    enable_pdf: bool = True

    @classmethod
    def from_env(cls):
        service_root = Path(__file__).resolve().parents[1]
        return cls(
            data_root=Path(os.environ.get("BIBHELPER_DATA_ROOT", "/data/bibhelper")).expanduser(),
            job_retention_days=int(os.environ.get("JOB_RETENTION_DAYS", "7")),
            max_upload_files=int(os.environ.get("MAX_UPLOAD_FILES", "5")),
            max_upload_bytes=int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))),
            max_concurrent_jobs=int(os.environ.get("MAX_CONCURRENT_JOBS", "1")),
            shortcut_api_token=os.environ.get("SHORTCUT_API_TOKEN", "change-me-token"),
            web_admin_password=os.environ.get("WEB_ADMIN_PASSWORD", "change-me-password"),
            session_secret=os.environ.get("SESSION_SECRET", "change-me-session-secret"),
            templates_dir=service_root / "templates",
            static_dir=service_root / "static",
            enable_pdf=os.environ.get("SERVICE_ENABLE_PDF", "1") == "1",
        )

    @property
    def jobs_root(self) -> Path:
        return self.data_root / "jobs"


TEMPLATE_DEFINITIONS = {
    "savings_single": {
        "filename": "template_savings_standalone.docx",
        "label": "储蓄险单独总结书模板",
        "description": "储蓄险单方案总结书模板。",
        "convertible": True,
    },
    "savings_comparison": {
        "filename": "template_savings_comparison.docx",
        "label": "储蓄险对比总结书模板",
        "description": "储蓄险双方案对比总结书模板。",
        "convertible": True,
    },
    "savings_single_45": {
        "filename": "template_savings_standalone_45.docx",
        "label": "储蓄险 45 岁专用模板",
        "description": "45 岁及以上客户的储蓄险单方案模板。",
        "convertible": True,
    },
    "critical_illness_single": {
        "filename": "template_ci_single.docx",
        "label": "重疾险单独总结书模板",
        "description": "重疾险单方案总结书模板。",
        "convertible": True,
    },
    "savings_overlay": {
        "filename": "aia_annotation_overlay.png",
        "label": "储蓄险投资总览图叠加模板",
        "description": "用于投资总览图标注叠加的 PNG 资源。",
        "convertible": False,
    },
}


class TemplateStore:
    TEMPLATE_HTML_CSS = """
body {
  margin: 0;
  background: #f5f1e8;
  color: #1f2933;
  font-family: "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", sans-serif;
}
.preview-banner {
  background: #0f766e;
  color: #fff;
  padding: 12px 18px;
  font-size: 14px;
  letter-spacing: 0.02em;
}
.template-document {
  max-width: 900px;
  margin: 24px auto;
  padding: 32px 36px;
  background: #fffdf8;
  box-shadow: 0 12px 36px rgba(31, 41, 51, 0.08);
  border-radius: 18px;
}
.template-document table {
  width: 100%;
  border-collapse: collapse;
  margin: 16px 0;
}
.template-document td,
.template-document th {
  border: 1px solid #d9d3c8;
  padding: 8px 10px;
  vertical-align: top;
}
.template-document p,
.template-document li {
  line-height: 1.75;
}
.template-document h1,
.template-document h2,
.template-document h3 {
  line-height: 1.3;
}
""".strip()

    def __init__(self, root: Path, default_root: Path):
        self.root = root
        self.default_root = default_root
        self.current_dir = self.root / "current"
        self.history_dir = self.root / "history"
        self.current_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def template_definition(self, template_id: str) -> dict:
        template = TEMPLATE_DEFINITIONS.get(template_id)
        if not template:
            raise KeyError(template_id)
        return template

    def current_path(self, template_id: str) -> Path:
        definition = self.template_definition(template_id)
        return self.current_dir / definition["filename"]

    def current_html_path(self, template_id: str) -> Path:
        return self._asset_paths(self.current_dir, template_id)["html"]

    def current_meta_path(self, template_id: str) -> Path:
        return self._asset_paths(self.current_dir, template_id)["meta"]

    def current_preview_path(self, template_id: str) -> Path:
        return self._asset_paths(self.current_dir, template_id)["preview"]

    def default_path(self, template_id: str) -> Path:
        definition = self.template_definition(template_id)
        return self.default_root / definition["filename"]

    def history_path(self, template_id: str) -> Path:
        path = self.history_dir / template_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def history_version_path(self, template_id: str, version_name: str) -> Path:
        return self.history_path(template_id) / version_name

    def _asset_paths(self, base_dir: Path, template_id: str) -> dict[str, Path]:
        definition = self.template_definition(template_id)
        stem = Path(definition["filename"]).stem
        return {
            "source": base_dir / definition["filename"],
            "html": base_dir / f"{stem}.html",
            "meta": base_dir / f"{stem}.meta.json",
            "preview": base_dir / f"{stem}.preview.html",
        }

    def _load_meta(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_meta(self, path: Path, payload: dict):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def ensure_defaults(self):
        for template_id in TEMPLATE_DEFINITIONS:
            current_path = self.current_path(template_id)
            if current_path.exists():
                self._refresh_generated_assets(template_id)
                continue
            source = self.default_path(template_id)
            if source.exists():
                shutil.copy2(source, current_path)
                self._refresh_generated_assets(template_id)

    def _serialize_file(self, path: Path) -> dict:
        stat = path.stat()
        return {
            "name": path.name,
            "size": stat.st_size,
            "updated_at": isoformat(datetime.fromtimestamp(stat.st_mtime, timezone.utc)),
        }

    def _serialize_current(self, template_id: str) -> dict:
        definition = self.template_definition(template_id)
        assets = self._asset_paths(self.current_dir, template_id)
        meta = self._load_meta(assets["meta"]) or {}
        return {
            "id": template_id,
            "filename": definition["filename"],
            "label": definition["label"],
            "description": definition["description"],
            "convertible": definition["convertible"],
            "current": self._serialize_file(assets["source"]) if assets["source"].exists() else None,
            "current_html": self._serialize_file(assets["html"]) if assets["html"].exists() else None,
            "preview": self._serialize_file(assets["preview"]) if assets["preview"].exists() else None,
            "meta_file": self._serialize_file(assets["meta"]) if assets["meta"].exists() else None,
            "current_meta": meta,
            "conversion_status": meta.get("conversion_status", "not_applicable"),
            "warnings": meta.get("warnings", []),
            "placeholder_summary": meta.get("placeholder_summary", []),
            "history": self._list_history_versions(template_id),
        }

    def _serialize_history_version(self, template_id: str, version_path: Path) -> dict | None:
        if version_path.is_file():
            source = self._serialize_file(version_path)
            return {
                "name": version_path.name,
                "source": source,
                "html": None,
                "preview": None,
                "meta_file": None,
                "meta": None,
                "conversion_status": "legacy",
                "warnings": [],
                "placeholder_summary": [],
                "updated_at": source["updated_at"],
                "sort_key": source["updated_at"],
            }
        if not version_path.is_dir():
            return None
        assets = self._asset_paths(version_path, template_id)
        if not assets["source"].exists():
            return None
        source = self._serialize_file(assets["source"])
        meta = self._load_meta(assets["meta"]) or {}
        return {
            "name": version_path.name,
            "source": source,
            "html": self._serialize_file(assets["html"]) if assets["html"].exists() else None,
            "preview": self._serialize_file(assets["preview"]) if assets["preview"].exists() else None,
            "meta_file": self._serialize_file(assets["meta"]) if assets["meta"].exists() else None,
            "meta": meta,
            "conversion_status": meta.get("conversion_status", "not_applicable"),
            "warnings": meta.get("warnings", []),
            "placeholder_summary": meta.get("placeholder_summary", []),
            "updated_at": meta.get("updated_at", source["updated_at"]),
            "sort_key": meta.get("updated_at", source["updated_at"]),
        }

    def _list_history_versions(self, template_id: str) -> list[dict]:
        entries = []
        for path in self.history_path(template_id).iterdir():
            item = self._serialize_history_version(template_id, path)
            if item:
                entries.append(item)
        entries.sort(key=lambda item: item["sort_key"], reverse=True)
        for item in entries:
            item.pop("sort_key", None)
        return entries

    def list_templates(self) -> list[dict]:
        return [self._serialize_current(template_id) for template_id in TEMPLATE_DEFINITIONS]

    def get_template(self, template_id: str) -> dict:
        return self._serialize_current(template_id)

    def _archive_current(self, template_id: str):
        assets = self._asset_paths(self.current_dir, template_id)
        if not assets["source"].exists():
            return None
        timestamp = utc_now().strftime("%Y%m%dT%H%M%S%f+0800")
        version_dir = self.history_path(template_id) / timestamp
        version_dir.mkdir(parents=True, exist_ok=True)
        for path in assets.values():
            if path.exists():
                shutil.copy2(path, version_dir / path.name)
        return version_dir

    def _extract_placeholders(self, source_path: Path) -> list[str]:
        if source_path.suffix.lower() != ".docx":
            return []
        try:
            with zipfile.ZipFile(source_path) as archive:
                xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
        except Exception:
            return []
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml))
        matches = re.findall(r"\{[^{}]+\}", text)
        return sorted(set(matches))

    def _wrap_html(self, title: str, body_html: str) -> str:
        return (
            "<!doctype html>\n"
            '<html lang="zh-CN">\n'
            "<head>\n"
            '  <meta charset="utf-8">\n'
            '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"  <title>{escape(title)}</title>\n"
            "  <style>\n"
            f"{self.TEMPLATE_HTML_CSS}\n"
            "  </style>\n"
            "</head>\n"
            "<body>\n"
            '  <main class="template-document">\n'
            f"{body_html}\n"
            "  </main>\n"
            "</body>\n"
            "</html>\n"
        )

    def _sample_placeholder_value(self, placeholder: str) -> str:
        key = placeholder.strip("{}").lower()
        if "name" in key:
            return "示例客户"
        if "age" in key:
            return "35"
        if "gender" in key:
            return "男"
        if "smoke" in key:
            return "非吸烟"
        if "payment_term" in key:
            return "5"
        if "years_withdraw" in key:
            return "20"
        if "premium" in key:
            return "12,000"
        if "withdraw" in key:
            return "8,800"
        if "cashout" in key:
            return "160,000"
        if "balance" in key:
            return "210,000"
        if "coverage" in key:
            return "500,000"
        return "示例值"

    def _build_preview_html(self, html_text: str, placeholders: list[str]) -> str:
        preview_html = html_text
        for placeholder in placeholders:
            preview_html = preview_html.replace(placeholder, self._sample_placeholder_value(placeholder))
        banner = '<div class="preview-banner">模板预览：以下内容为示例数据，仅用于检查转换效果。</div>'
        return preview_html.replace("<body>", f"<body>\n{banner}\n", 1)

    def _build_conversion_meta(
        self,
        template_id: str,
        source_path: Path,
        version_id: str,
        status: str,
        warnings: list[str],
        placeholders: list[str],
        html_path: Path | None,
        preview_path: Path | None,
        engine: str | None,
        fallback_to_docx: bool,
    ) -> dict:
        updated_at = isoformat(datetime.fromtimestamp(source_path.stat().st_mtime, timezone.utc))
        return {
            "template_id": template_id,
            "source_filename": source_path.name,
            "source_format": source_path.suffix.lower().lstrip("."),
            "updated_at": updated_at,
            "conversion_status": status,
            "conversion_engine": engine,
            "warnings": warnings,
            "html_filename": html_path.name if html_path and html_path.exists() else None,
            "preview_filename": preview_path.name if preview_path and preview_path.exists() else None,
            "fallback_to_docx": fallback_to_docx,
            "placeholder_summary": placeholders,
            "version_id": version_id,
        }

    def _refresh_generated_assets(self, template_id: str):
        definition = self.template_definition(template_id)
        assets = self._asset_paths(self.current_dir, template_id)
        source_path = assets["source"]
        if not source_path.exists():
            for path in assets.values():
                if path.exists():
                    path.unlink()
            return

        placeholders = self._extract_placeholders(source_path)
        warnings: list[str] = []
        status = "not_applicable"
        engine = None
        fallback_to_docx = False

        if not definition["convertible"]:
            for key in ("html", "preview"):
                if assets[key].exists():
                    assets[key].unlink()
        else:
            engine = "mammoth"
            if mammoth is None:
                status = "failed"
                warnings.append("未安装 mammoth，无法生成 HTML。")
                fallback_to_docx = True
            else:
                try:
                    with source_path.open("rb") as handle:
                        result = mammoth.convert_to_html(handle)
                    html_text = self._wrap_html(definition["label"], result.value)
                    assets["html"].write_text(html_text, encoding="utf-8")
                    assets["preview"].write_text(
                        self._build_preview_html(html_text, placeholders),
                        encoding="utf-8",
                    )
                    warnings = [f"{message.type}: {message.message}" for message in result.messages]
                    status = "warning" if warnings else "success"
                except Exception as exc:
                    status = "failed"
                    warnings.append(f"转换失败: {exc}")
                    fallback_to_docx = True
            if status == "failed":
                for key in ("html", "preview"):
                    if assets[key].exists():
                        assets[key].unlink()

        meta = self._build_conversion_meta(
            template_id=template_id,
            source_path=source_path,
            version_id="current",
            status=status,
            warnings=warnings,
            placeholders=placeholders,
            html_path=assets["html"],
            preview_path=assets["preview"],
            engine=engine,
            fallback_to_docx=fallback_to_docx,
        )
        self._write_meta(assets["meta"], meta)

    def save_upload(self, template_id: str, upload: UploadFile):
        definition = self.template_definition(template_id)
        filename = Path(upload.filename or "").name
        expected_suffix = Path(definition["filename"]).suffix.lower()
        if Path(filename).suffix.lower() != expected_suffix:
            raise ValueError(f"模板文件必须是 {expected_suffix} 格式。")
        content = upload.file.read()
        if not content:
            raise ValueError("上传文件不能为空。")
        self._archive_current(template_id)
        current_path = self.current_path(template_id)
        tmp_path = current_path.with_suffix(current_path.suffix + ".tmp")
        tmp_path.write_bytes(content)
        tmp_path.replace(current_path)
        self._refresh_generated_assets(template_id)
        return current_path

    def restore_history(self, template_id: str, version_name: str):
        history_root = self.history_path(template_id).resolve()
        version_path = (history_root / version_name).resolve()
        if not str(version_path).startswith(str(history_root)) or not version_path.exists():
            raise FileNotFoundError(version_name)
        self._archive_current(template_id)
        if version_path.is_file():
            shutil.copy2(version_path, self.current_path(template_id))
            self._refresh_generated_assets(template_id)
            return self.current_path(template_id)
        current_assets = self._asset_paths(self.current_dir, template_id)
        history_assets = self._asset_paths(version_path, template_id)
        if not history_assets["source"].exists():
            raise FileNotFoundError(version_name)
        shutil.copy2(history_assets["source"], current_assets["source"])
        for key in ("html", "meta", "preview"):
            if history_assets[key].exists():
                shutil.copy2(history_assets[key], current_assets[key])
            elif current_assets[key].exists():
                current_assets[key].unlink()
        if self.template_definition(template_id)["convertible"] and not current_assets["meta"].exists():
            self._refresh_generated_assets(template_id)
        return self.current_path(template_id)

    def current_asset_path(self, template_id: str, asset_type: str) -> Path:
        assets = self._asset_paths(self.current_dir, template_id)
        path = assets[asset_type]
        if not path.exists():
            raise FileNotFoundError(path.name)
        return path

    def history_asset_path(self, template_id: str, version_name: str, asset_type: str) -> Path:
        history_root = self.history_path(template_id).resolve()
        version_path = (history_root / version_name).resolve()
        if not str(version_path).startswith(str(history_root)) or not version_path.exists():
            raise FileNotFoundError(version_name)
        if version_path.is_file():
            if asset_type != "source":
                raise FileNotFoundError(version_name)
            return version_path
        path = self._asset_paths(version_path, template_id)[asset_type]
        if not path.exists():
            raise FileNotFoundError(version_name)
        return path

class JobStore:
    def __init__(self, jobs_root: Path, retention_days: int):
        self.jobs_root = jobs_root
        self.retention_days = retention_days
        self.jobs_root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def incoming_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "incoming"

    def work_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "work"

    def output_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "output"

    def zip_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "result.zip"

    def job_json_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def process_log_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "process.log"

    def create_job(self, original_filenames: list[str]) -> dict:
        job_id = uuid.uuid4().hex
        created_at = utc_now()
        expires_at = created_at + timedelta(days=self.retention_days)
        job_dir = self.job_dir(job_id)
        self.incoming_dir(job_id).mkdir(parents=True, exist_ok=True)
        self.work_dir(job_id).mkdir(parents=True, exist_ok=True)
        self.output_dir(job_id).mkdir(parents=True, exist_ok=True)
        self.process_log_path(job_id).touch()
        job_data = {
            "job_id": job_id,
            "status": "processing",
            "created_at": isoformat(created_at),
            "expires_at": isoformat(expires_at),
            "original_filenames": original_filenames,
            "result_filename": "result.zip",
            "artifacts": [],
            "error_message": None,
            "warnings": [],
            "classified": {},
            "tasks": [],
        }
        self.write_job(job_id, job_data)
        return job_data

    def write_job(self, job_id: str, data: dict):
        self.job_json_path(job_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_job(self, job_id: str) -> dict:
        path = self.job_json_path(job_id)
        if not path.exists():
            raise FileNotFoundError(job_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def update_job(self, job_id: str, **updates):
        data = self.load_job(job_id)
        data.update(updates)
        self.write_job(job_id, data)
        return data

    def save_uploads(self, job_id: str, uploads: list[tuple[str, bytes]]) -> list[Path]:
        saved_paths = []
        seen = {}
        for filename, content in uploads:
            base_name = Path(filename).name or "upload.pdf"
            seen[base_name] = seen.get(base_name, 0) + 1
            if seen[base_name] > 1:
                safe_name = f"{seen[base_name]}_{base_name}"
            else:
                safe_name = base_name
            target = self.incoming_dir(job_id) / safe_name
            target.write_bytes(content)
            saved_paths.append(target)
        return saved_paths

    def list_recent_jobs(self) -> list[dict]:
        jobs = []
        cutoff = utc_now() - timedelta(days=self.retention_days)
        for job_json in self.jobs_root.glob("*/job.json"):
            try:
                data = json.loads(job_json.read_text(encoding="utf-8"))
            except Exception:
                continue
            created_at = parse_timestamp(data.get("created_at"))
            if created_at and created_at >= cutoff:
                jobs.append(data)
        jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return jobs

    def cleanup_expired(self):
        now = utc_now()
        for job_json in self.jobs_root.glob("*/job.json"):
            try:
                data = json.loads(job_json.read_text(encoding="utf-8"))
            except Exception:
                continue
            expires_at = parse_timestamp(data.get("expires_at"))
            if expires_at and expires_at < now:
                shutil.rmtree(job_json.parent, ignore_errors=True)


def serialize_artifact(artifact: GeneratedArtifact) -> dict:
    return asdict(artifact)


def serialize_task(task: dict) -> dict:
    return {
        "type": task.get("type"),
        "mode": task.get("mode"),
        "files": [Path(file_path).name for file_path in task.get("files", [])],
    }


def build_result_filename(artifacts: list[GeneratedArtifact], original_filenames: list[str]) -> str:
    customer_name = next((artifact.customer_name for artifact in artifacts if artifact.customer_name), None)
    base_name = customer_name or "保险总结书"
    safe_name = "".join("_" if char in '<>:"/\\|?*\n\r\t' else char for char in base_name).strip(" .")
    if not safe_name:
        safe_name = "保险总结书"
    if customer_name:
        return f"{safe_name}保险总结书.zip"
    return f"{safe_name}.zip"


def ensure_pdf_uploads(files: list[UploadFile], max_upload_files: int, max_upload_bytes: int):
    if not files:
        raise HTTPException(status_code=400, detail="至少上传 1 个 PDF 文件。")
    if len(files) > max_upload_files:
        raise HTTPException(status_code=400, detail=f"单次最多上传 {max_upload_files} 个文件。")
    uploads = []
    total_size = 0
    for upload in files:
        filename = Path(upload.filename or "").name
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="仅支持 PDF 文件。")
        content = upload.file.read()
        if not content.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="文件内容不是合法 PDF。")
        total_size += len(content)
        if total_size > max_upload_bytes:
            raise HTTPException(status_code=400, detail="上传总大小超过限制。")
        uploads.append((filename, content))
    return uploads


def create_zip_from_output(output_dir: Path, zip_path: Path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(output_dir.rglob("*")):
            if file_path.is_file():
                archive.write(file_path, arcname=str(file_path.relative_to(output_dir)))


def try_acquire_slot(app: FastAPI):
    semaphore = app.state.job_semaphore
    acquired = semaphore.acquire(blocking=False)
    if not acquired:
        raise HTTPException(status_code=429, detail="当前有任务正在处理中，请稍后再试。")


def release_slot(app: FastAPI):
    app.state.job_semaphore.release()


def process_job(app: FastAPI, job_id: str) -> dict:
    store: JobStore = app.state.job_store
    config: ServiceConfig = app.state.service_config
    processor: Callable[[RunOptions], RunResult] = app.state.processor
    template_store: TemplateStore = app.state.template_store
    job_data = store.load_job(job_id)
    input_files = sorted(store.incoming_dir(job_id).glob("*.pdf"))
    result = processor(
        RunOptions(
            input_files=input_files,
            workspace_dir=store.work_dir(job_id),
            output_root=store.output_dir(job_id),
            enable_pdf=config.enable_pdf,
            interactive=False,
            template_root=template_store.current_dir,
        )
    )
    create_zip_from_output(store.output_dir(job_id), store.zip_path(job_id))
    result_filename = build_result_filename(result.artifacts, job_data.get("original_filenames", []))
    return store.update_job(
        job_id,
        status="completed",
        result_filename=result_filename,
        artifacts=[serialize_artifact(artifact) for artifact in result.artifacts],
        warnings=result.warnings,
        classified=result.classified,
        tasks=[serialize_task(task) for task in result.tasks],
        error_message=None,
    )


def process_job_background(app: FastAPI, job_id: str):
    try:
        process_job(app, job_id)
    except Exception as exc:
        app.state.job_store.update_job(job_id, status="failed", error_message=str(exc))
    finally:
        release_slot(app)


def require_api_token(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None
    if token != request.app.state.service_config.shortcut_api_token:
        raise HTTPException(status_code=401, detail="鉴权失败。")


def require_web_session(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=303)
    return None


def resolve_output_file(base_dir: Path, relative_path: str) -> Path:
    target = (base_dir / relative_path).resolve()
    if not str(target).startswith(str(base_dir.resolve())) or not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在。")
    return target


def get_template_or_404(template_store: TemplateStore, template_id: str) -> dict:
    try:
        return template_store.get_template(template_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="模板不存在。")


def create_app(config: ServiceConfig | None = None, processor: Callable[[RunOptions], RunResult] | None = None) -> FastAPI:
    config = config or ServiceConfig.from_env()
    processor = processor or run_pipeline
    config.data_root.mkdir(parents=True, exist_ok=True)
    job_store = JobStore(config.jobs_root, config.job_retention_days)
    template_store = TemplateStore(config.data_root / "templates", REPO_ROOT / "packages" / "bib_core" / "src" / "bib_core" / "resources")
    template_store.ensure_defaults()
    cleanup_stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        job_store.cleanup_expired()
        template_store.ensure_defaults()

        def cleanup_loop():
            while not cleanup_stop.wait(3600):
                job_store.cleanup_expired()

        thread = threading.Thread(target=cleanup_loop, daemon=True)
        thread.start()
        app.state.cleanup_thread = thread
        try:
            yield
        finally:
            cleanup_stop.set()

    app = FastAPI(title="BIBhelper Service", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret,
        same_site="lax",
        max_age=12 * 60 * 60,
    )
    app.mount("/static", StaticFiles(directory=str(config.static_dir)), name="static")

    templates = Jinja2Templates(directory=str(config.templates_dir))
    app.state.service_config = config
    app.state.job_store = job_store
    app.state.template_store = template_store
    app.state.job_semaphore = threading.BoundedSemaphore(config.max_concurrent_jobs)
    app.state.processor = processor

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def root():
        return RedirectResponse(url="/jobs", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request, password: str = Form(...)):
        if password != config.web_admin_password:
            return templates.TemplateResponse(request, "login.html", {"error": "密码错误。"}, status_code=401)
        request.session["authenticated"] = True
        return RedirectResponse(url="/jobs", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/upload", response_class=HTMLResponse)
    def upload_page(request: Request):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        return templates.TemplateResponse(request, "upload.html", {"max_upload_files": config.max_upload_files})

    @app.get("/templates", response_class=HTMLResponse)
    def templates_page(request: Request):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        items = app.state.template_store.list_templates()
        return templates.TemplateResponse(
            request,
            "templates.html",
            {
                "templates_data": items,
                "message": request.query_params.get("message"),
            },
        )

    @app.get("/templates/{template_id}", response_class=HTMLResponse)
    def template_detail_page(request: Request, template_id: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        template_data = get_template_or_404(app.state.template_store, template_id)
        return templates.TemplateResponse(
            request,
            "template_detail.html",
            {
                "template_data": template_data,
                "message": request.query_params.get("message"),
            },
        )

    @app.get("/templates/{template_id}/download")
    def template_download(request: Request, template_id: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        template_data = get_template_or_404(app.state.template_store, template_id)
        path = app.state.template_store.current_path(template_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="模板不存在。")
        return FileResponse(path, filename=template_data["filename"])

    @app.get("/templates/{template_id}/html")
    def template_html_download(request: Request, template_id: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        get_template_or_404(app.state.template_store, template_id)
        try:
            path = app.state.template_store.current_asset_path(template_id, "html")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="HTML 模板不存在。")
        return FileResponse(path, filename=path.name, media_type="text/html")

    @app.get("/templates/{template_id}/meta")
    def template_meta_download(request: Request, template_id: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        get_template_or_404(app.state.template_store, template_id)
        try:
            path = app.state.template_store.current_asset_path(template_id, "meta")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="模板元数据不存在。")
        return FileResponse(path, filename=path.name, media_type="application/json")

    @app.get("/templates/{template_id}/preview", response_class=HTMLResponse)
    def template_preview(request: Request, template_id: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        get_template_or_404(app.state.template_store, template_id)
        try:
            path = app.state.template_store.current_asset_path(template_id, "preview")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="预览不存在。")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/templates/{template_id}/history/{version_name}/download")
    def template_history_download(request: Request, template_id: str, version_name: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        get_template_or_404(app.state.template_store, template_id)
        try:
            path = app.state.template_store.history_asset_path(template_id, version_name, "source")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="历史模板不存在。")
        return FileResponse(path, filename=path.name)

    @app.get("/templates/{template_id}/history/{version_name}/html")
    def template_history_html_download(request: Request, template_id: str, version_name: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        get_template_or_404(app.state.template_store, template_id)
        try:
            path = app.state.template_store.history_asset_path(template_id, version_name, "html")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="历史 HTML 模板不存在。")
        return FileResponse(path, filename=path.name, media_type="text/html")

    @app.get("/templates/{template_id}/history/{version_name}/meta")
    def template_history_meta_download(request: Request, template_id: str, version_name: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        get_template_or_404(app.state.template_store, template_id)
        try:
            path = app.state.template_store.history_asset_path(template_id, version_name, "meta")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="历史模板元数据不存在。")
        return FileResponse(path, filename=path.name, media_type="application/json")

    @app.get("/templates/{template_id}/history/{version_name}/preview", response_class=HTMLResponse)
    def template_history_preview(request: Request, template_id: str, version_name: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        get_template_or_404(app.state.template_store, template_id)
        try:
            path = app.state.template_store.history_asset_path(template_id, version_name, "preview")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="历史预览不存在。")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.post("/templates/{template_id}/upload")
    def template_upload(request: Request, template_id: str, file: UploadFile = File(...)):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        get_template_or_404(app.state.template_store, template_id)
        try:
            app.state.template_store.save_upload(template_id, file)
        except ValueError as exc:
            return RedirectResponse(
                url=f"/templates/{template_id}?message={str(exc)}",
                status_code=303,
            )
        return RedirectResponse(url=f"/templates/{template_id}?message=模板已更新", status_code=303)

    @app.post("/templates/{template_id}/restore")
    def template_restore(request: Request, template_id: str, version_name: str = Form(...)):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        get_template_or_404(app.state.template_store, template_id)
        try:
            app.state.template_store.restore_history(template_id, version_name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="历史模板不存在。")
        return RedirectResponse(url=f"/templates/{template_id}?message=已恢复历史模板", status_code=303)

    @app.post("/upload")
    def upload_submit(request: Request, background_tasks: BackgroundTasks, files: list[UploadFile] = File(...)):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        uploads = ensure_pdf_uploads(files, config.max_upload_files, config.max_upload_bytes)
        try_acquire_slot(app)
        job_data = None
        try:
            job_data = app.state.job_store.create_job([filename for filename, _ in uploads])
            app.state.job_store.save_uploads(job_data["job_id"], uploads)
            background_tasks.add_task(process_job_background, app, job_data["job_id"])
            return RedirectResponse(url=f"/jobs/{job_data['job_id']}", status_code=303)
        except Exception:
            if job_data:
                app.state.job_store.update_job(job_data["job_id"], status="failed", error_message="任务初始化失败。")
            release_slot(app)
            raise

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        jobs = app.state.job_store.list_recent_jobs()
        return templates.TemplateResponse(request, "jobs.html", {"jobs": jobs})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail_page(request: Request, job_id: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        try:
            job = app.state.job_store.load_job(job_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="任务不存在。")
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            {
                "job": job,
                "refresh_seconds": 3 if job.get("status") == "processing" else None,
            },
        )

    @app.get("/jobs/{job_id}/download")
    def web_download_job(request: Request, job_id: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        try:
            job_data = app.state.job_store.load_job(job_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="任务不存在。")
        zip_path = app.state.job_store.zip_path(job_id)
        if not zip_path.exists():
            raise HTTPException(status_code=404, detail="结果文件不存在。")
        return FileResponse(
            zip_path,
            filename=job_data.get("result_filename") or "result.zip",
            media_type="application/zip",
        )

    @app.get("/jobs/{job_id}/artifacts/{artifact_name:path}")
    def web_download_artifact(request: Request, job_id: str, artifact_name: str):
        redirect = require_web_session(request)
        if redirect:
            return redirect
        try:
            app.state.job_store.load_job(job_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="任务不存在。")
        output_dir = app.state.job_store.output_dir(job_id)
        target = resolve_output_file(output_dir, artifact_name)
        return FileResponse(target, filename=target.name)

    @app.post("/api/v1/process")
    def api_process(files: list[UploadFile] = File(...), request: Request = None):
        require_api_token(request)
        uploads = ensure_pdf_uploads(files, config.max_upload_files, config.max_upload_bytes)
        try_acquire_slot(app)
        job_data = app.state.job_store.create_job([filename for filename, _ in uploads])
        try:
            app.state.job_store.save_uploads(job_data["job_id"], uploads)
            process_job(app, job_data["job_id"])
        except PipelineError as exc:
            app.state.job_store.update_job(job_data["job_id"], status="failed", error_message=str(exc))
            raise HTTPException(status_code=422, detail=str(exc))
        except HTTPException:
            raise
        except Exception as exc:
            app.state.job_store.update_job(job_data["job_id"], status="failed", error_message=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            release_slot(app)
        zip_path = app.state.job_store.zip_path(job_data["job_id"])
        job_data = app.state.job_store.load_job(job_data["job_id"])
        return FileResponse(
            zip_path,
            filename=job_data.get("result_filename") or "result.zip",
            media_type="application/zip",
            headers={"X-Job-Id": job_data["job_id"]},
        )

    @app.get("/api/v1/jobs/{job_id}")
    def api_job_status(job_id: str, request: Request):
        require_api_token(request)
        try:
            return JSONResponse(app.state.job_store.load_job(job_id))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="任务不存在。")

    @app.get("/api/v1/jobs/{job_id}/download")
    def api_job_download(job_id: str, request: Request):
        require_api_token(request)
        try:
            job_data = app.state.job_store.load_job(job_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="任务不存在。")
        zip_path = app.state.job_store.zip_path(job_id)
        if not zip_path.exists():
            raise HTTPException(status_code=404, detail="结果文件不存在。")
        return FileResponse(
            zip_path,
            filename=job_data.get("result_filename") or "result.zip",
            media_type="application/zip",
            headers={"X-Job-Id": job_id},
        )

    return app


app = create_app()
