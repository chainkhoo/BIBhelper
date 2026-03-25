FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV BIBHELPER_DATA_ROOT=/data/bibhelper

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libreoffice \
       fonts-noto-cjk \
       libcairo2 \
       libpango-1.0-0 \
       libpangocairo-1.0-0 \
       libgdk-pixbuf-2.0-0 \
       libffi-dev \
       shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

COPY pyproject.toml /app/pyproject.toml
COPY README.md /app/README.md
COPY aia.py /app/aia.py
COPY packages /app/packages
COPY apps /app/apps

RUN pip install --no-cache-dir --no-deps .

EXPOSE 8000
VOLUME ["/data/bibhelper"]

CMD ["uvicorn", "apps.service.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
