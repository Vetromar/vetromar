# The cloud workspace service ONLY (cloud/ + its one lightweight import,
# vetromar/workspace/wire.py). The heavy vetromar deps (fastembed, torch,
# anthropic, ...) are never imported server-side and stay out of the image.
FROM python:3.13-slim

WORKDIR /app

COPY cloud/requirements.txt cloud/requirements.txt
RUN pip install --no-cache-dir -r cloud/requirements.txt

COPY cloud/ cloud/
COPY vetromar/ vetromar/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# Railway injects PORT; the 8787 fallback keeps `docker run` local-testable.
CMD ["sh", "-c", "python -m cloud --host 0.0.0.0 --port ${PORT:-8787}"]
