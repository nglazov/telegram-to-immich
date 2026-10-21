FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd -g 1028 photos && useradd -u 1028 -g photos --no-create-home photos
COPY telegram_photos ./telegram_photos
# Source archives can create parent directories with a restrictive umask.
# Runtime code and static assets must be readable by the non-root app user.
RUN chmod -R a+rX /app/telegram_photos
ENV STATE_DIR=/state PHOTOS_DIR=/photos PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 1028:1028
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"
CMD ["uvicorn", "telegram_photos.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
