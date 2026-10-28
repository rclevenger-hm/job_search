FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 JOBRADAR_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 jobradar \
    && useradd --uid 10001 --gid 10001 --create-home jobradar \
    && mkdir /data && chown jobradar:jobradar /data
COPY request_limits.py resumes.py curated.py board_repairs.py board-audit.json extra_sources.py core.py scanner.py source_health.py web.py board-directory.json ./
COPY templates ./templates
COPY static ./static
USER jobradar
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "web.py"]
