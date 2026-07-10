FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install the package in editable mode so relative-path assumptions
# (e.g. jobradar/paths.py resolving DATA_DIR) match local dev.
COPY pyproject.toml ./
COPY jobradar ./jobradar

RUN pip install --no-cache-dir -e .

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/')" || exit 1

CMD ["jobradar", "serve", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
