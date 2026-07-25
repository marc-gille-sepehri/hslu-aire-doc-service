FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    DOCLING_ARTIFACTS_PATH=/models

WORKDIR /app

# System libs Torch / Docling need at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the Docling models into the image so the first request doesn't download them.
# (Best-effort: adjust to the pinned Docling version if the API changes.)
RUN python -c "from docling.utils.model_downloader import download_models; download_models()" \
    || docling-tools models download \
    || true

COPY app ./app

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
