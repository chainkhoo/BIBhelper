from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
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


def utc_now():
    return datetime.now(timezone.utc)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


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
    input_files = sorted(store.incoming_dir(job_id).glob("*.pdf"))
    result = processor(
        RunOptions(
            input_files=input_files,
            workspace_dir=store.work_dir(job_id),
            output_root=store.output_dir(job_id),
            enable_pdf=config.enable_pdf,
            interactive=False,
        )
    )
    create_zip_from_output(store.output_dir(job_id), store.zip_path(job_id))
    return store.update_job(
        job_id,
        status="completed",
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


def create_app(config: ServiceConfig | None = None, processor: Callable[[RunOptions], RunResult] | None = None) -> FastAPI:
    config = config or ServiceConfig.from_env()
    processor = processor or run_pipeline
    config.data_root.mkdir(parents=True, exist_ok=True)
    job_store = JobStore(config.jobs_root, config.job_retention_days)
    cleanup_stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        job_store.cleanup_expired()

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
            app.state.job_store.load_job(job_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="任务不存在。")
        zip_path = app.state.job_store.zip_path(job_id)
        if not zip_path.exists():
            raise HTTPException(status_code=404, detail="结果文件不存在。")
        return FileResponse(zip_path, filename="result.zip", media_type="application/zip")

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
        return FileResponse(
            zip_path,
            filename="result.zip",
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
            app.state.job_store.load_job(job_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="任务不存在。")
        zip_path = app.state.job_store.zip_path(job_id)
        if not zip_path.exists():
            raise HTTPException(status_code=404, detail="结果文件不存在。")
        return FileResponse(
            zip_path,
            filename="result.zip",
            media_type="application/zip",
            headers={"X-Job-Id": job_id},
        )

    return app


app = create_app()
