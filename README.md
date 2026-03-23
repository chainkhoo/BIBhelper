# BIBhelper

BIBhelper 用于处理 AIA 建议书 PDF，并生成面向客户的总结书。

当前受支持范围：
- 储蓄险
- 重疾险

当前不包含教育金逻辑。

## 当前结构

- `packages/bib_core/`
  共享核心包，负责 PDF 提取、分类、任务构建、模板填充、PDF 转换和投资总览图生成
- `apps/cli/`
  本地命令行入口
- `apps/service/`
  FastAPI 在线服务，同时提供 API 和简单网页后台
- `aia.py`
  兼容旧入口，内部转调 `apps/cli`
- `tests/test_aia.py`
  核心逻辑和服务层自动化测试
- `deploy/`
  Dockerfile 和 `docker-compose.yml`

## 功能概览

- 自动识别上传或输入目录中的 PDF，并分类为储蓄险或重疾险
- 为储蓄险生成单独总结书，并在同一客户存在两个方案时自动生成对比总结书
- 为重疾险生成单独总结书
- 根据模板填充 Word 文档，并在环境支持时转换为 PDF
- 为储蓄险生成带标注的投资总览图
- 提供 HTTP API，支持 iOS Shortcut 上传并直接返回 ZIP
- 提供简单网页后台，支持登录、上传、查看历史记录、下载 ZIP 和单文件

## 运行要求

- Python 3.11+ 用于在线服务和 Docker 环境
- Python 3.9+ 可运行当前本地 CLI 与测试
- 如需自动转 PDF，建议安装 LibreOffice
- 若需实时汇率，运行环境需要可访问外网；失败时会回退到默认汇率

## CLI 使用

兼容旧入口：

```bash
./aiahelper_pro/bin/python aia.py
```

也可以直接使用新的 CLI：

```bash
./aiahelper_pro/bin/python apps/cli/main.py --input-dir . --output-dir .
```

常用参数：

- `--non-interactive`
  跳过人工确认，直接按自动任务执行
- `--enable-pdf` / `--no-pdf`
  控制是否尝试转换 PDF
- `--usd-cny`
  手动指定美元兑人民币汇率

## 在线服务

本地启动：

```bash
uvicorn apps.service.app.main:app --host 0.0.0.0 --port 8000
```

主要 API：

- `POST /api/v1/process`
  `multipart/form-data`，重复字段名 `files`，Header 需要 `Authorization: Bearer <token>`
- `GET /api/v1/jobs/{job_id}`
- `GET /api/v1/jobs/{job_id}/download`
- `GET /healthz`

网页后台：

- `GET /login`
- `GET /upload`
- `GET /jobs`
- `GET /jobs/{job_id}`

## 关键环境变量

- `SHORTCUT_API_TOKEN`
  API Bearer Token
- `WEB_ADMIN_PASSWORD`
  网页后台登录密码
- `SESSION_SECRET`
  会话签名密钥
- `JOB_RETENTION_DAYS`
  结果保留天数，默认 `7`
- `MAX_UPLOAD_FILES`
  单次最大文件数，默认 `5`
- `MAX_UPLOAD_BYTES`
  单次上传总大小上限，默认 `52428800`
- `MAX_CONCURRENT_JOBS`
  同时处理的任务数，默认 `1`
- `BIBHELPER_DATA_ROOT`
  服务端任务目录，默认 `/data/bibhelper`

## Docker 部署

```bash
docker compose -f deploy/docker-compose.yml up --build
```

容器默认监听 `8000`，数据目录挂载到 `/data/bibhelper`。生产环境建议放在现有 Nginx 或 Caddy 后面，由反向代理处理 HTTPS、上传大小和超时。

## 运行测试

当前测试覆盖：

- 带空格英文名的姓名提取
- 扫描入口的姓名/年龄识别
- 储蓄险自动对比任务构建
- 当前范围不再包含教育金
- API 鉴权、上传校验、ZIP 下载
- 网页登录、上传、历史记录展示

执行命令：

```bash
./aiahelper_pro/bin/python -m unittest discover -s tests -v
```

## 已知约束

- PDF 解析依赖 AIA 建议书版式稳定，版式偏差过大时可能需要补规则
- 服务端当前为单用户、单实例设计，不包含账号体系和异步队列
- PDF 转换效果依赖 LibreOffice/本机转换环境，跨机器结果可能略有差异
