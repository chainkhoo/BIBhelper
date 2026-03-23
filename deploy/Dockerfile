FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV BIBHELPER_DATA_ROOT=/data/bibhelper

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
COPY packages /app/packages
COPY apps /app/apps
COPY aia.py /app/aia.py
COPY README.md /app/README.md

RUN pip install --no-cache-dir .

EXPOSE 8000
VOLUME ["/data/bibhelper"]

CMD ["uvicorn", "apps.service.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
