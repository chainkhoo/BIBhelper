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
  Dockerfile、`docker-compose.yml` 和 `docker-compose.ghcr.yml`

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
- Python 入口会自动读取 `deploy/.env.runtime` 和 `deploy/.env`
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
  结果保留天数，默认 `0`；设置为 `0` 表示永久保留，不自动清理
- `MAX_UPLOAD_FILES`
  单次最大文件数，默认 `5`
- `MAX_UPLOAD_BYTES`
  单次上传总大小上限，默认 `52428800`
- `MAX_CONCURRENT_JOBS`
  同时处理的任务数，默认 `1`
- `BIBHELPER_DATA_ROOT`
  服务端任务目录，默认 `/data/bibhelper`
- `BIBHELPER_HOST_DATA_ROOT`
  Docker 宿主机持久化目录，默认 `/opt/bibhelper-data`
- `EXCHANGE_RATE_API_KEY`
  ExchangeRate-API 的 key，用于优先获取 USD/CNY 汇率
- `EXCHANGERATE_HOST_API_KEY_PRIMARY`
  exchangerate.host 主 key
- `EXCHANGERATE_HOST_API_KEY_SECONDARY`
  exchangerate.host 备用 key
- `USD_CNY_RATE`
  所有在线渠道都失败时使用的默认汇率，默认 `6.9`

汇率获取顺序：

- 已配置的 `EXCHANGE_RATE_API_KEY`
- 已配置的 `EXCHANGERATE_HOST_API_KEY_PRIMARY`
- 已配置的 `EXCHANGERATE_HOST_API_KEY_SECONDARY`
- 无需 key 的公开渠道 Frankfurter
- 若全部失败，回退到 `USD_CNY_RATE` 或默认值 `6.9`

## Docker 部署

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

容器默认监听 `127.0.0.1:8000`，数据目录挂载到 `/data/bibhelper`。生产环境建议放在现有 Nginx 或 Caddy 后面，由反向代理处理 HTTPS、上传大小和超时。

当前 compose 默认使用宿主机目录挂载，并默认永久保留任务历史：

```text
/opt/bibhelper-data:/data/bibhelper
```

因此以下内容都会永久保存在服务器磁盘中，不会因为容器更新而消失：

- `jobs/`
- `templates/current/`
- `templates/history/`

当前线上生产链路固定为 `DOCX -> PDF`。仓库里保留了底层 `HTML -> PDF` 处理能力，用于后续继续修整 HTML 打印样式，但默认不会参与线上总结书生成，也不会在模板管理中自动生成 HTML/预览文件。

### 方案 B：服务器本地 build，但依赖层缓存

当前 `deploy/Dockerfile` 已按“依赖层”和“代码层”拆分：

- `requirements.txt` 先安装第三方依赖
- 业务代码后复制
- 最后执行 `pip install --no-deps .`

这样在 `requirements.txt` 不变时，修改页面、路由或核心逻辑只会重跑代码层，`pip install -r requirements.txt` 会直接命中 Docker 缓存。

常用命令：

```bash
cd /opt/bibhelper
git pull origin main
docker compose -f deploy/docker-compose.yml up -d --build
```

### 方案 C：GitHub Actions 构建镜像，服务器只 pull

仓库已包含 GHCR 发布工作流：

- 文件：`.github/workflows/docker-publish.yml`
- 触发：推送到 `main` 或推送 `v*` tag
- 镜像地址：`ghcr.io/chainkhoo/bibhelper:latest`

服务器端可直接使用：

```bash
docker compose -f deploy/docker-compose.ghcr.yml pull
docker compose -f deploy/docker-compose.ghcr.yml up -d
```

如果你已经在 1Panel 上使用编排部署，建议后续切到 `deploy/docker-compose.ghcr.yml`，这样服务器更新时只需要拉镜像，不再本地构建。

### 从旧 named volume 迁移到宿主机目录

如果你之前用的是 Docker named volume，而不是宿主机目录挂载，需要先把旧数据迁到 `/opt/bibhelper-data`：

```bash
docker volume ls | grep bibhelper-data
mkdir -p /opt/bibhelper-data
docker run --rm \
  -v <你的旧 volume 名称>:/from \
  -v /opt/bibhelper-data:/to \
  alpine sh -c 'cp -a /from/. /to/'
```

迁移完成后再启动新的 compose 文件。

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
