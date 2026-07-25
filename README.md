# hslu-aire-doc-service

A small FastAPI microservice for the AI@RE training portal:

- **Documents** (PDF, DOCX, PPTX, HTML, images, …) → **Markdown** via [Docling](https://github.com/DS4SD/docling).
- **Spreadsheets** (XLSX/XLS/XLSM) → a **rich descriptive JSON** analysis (per sheet: row count, per-column dtype / null / unique counts, numeric stats or top categorical values, plus a small sample) — more useful than flat Markdown.

It is called **server-to-server** by `hslu-aire-server` and is **not exposed publicly**. A shared bearer token (`SERVICE_TOKEN`) guards it.

## API

- `GET /health` → `{ "ok": true }` (App Runner health check).
- `POST /convert` — multipart form field `file`, header `Authorization: Bearer <SERVICE_TOKEN>`.
  Returns either
  `{ "kind": "markdown", "filename", "markdown" }` or
  `{ "kind": "excel", "filename", "excel": { sheets: [...] } }`.

## Config (env)

| var | default | meaning |
|---|---|---|
| `SERVICE_TOKEN` | *(empty)* | shared bearer token; if empty, auth is disabled (local dev only) |
| `MAX_UPLOAD_BYTES` | `31457280` (30 MB) | reject larger uploads |
| `DOCLING_ARTIFACTS_PATH` | `/models` | where Docling models live (baked into the image) |

## Local dev

The Excel path needs only light deps; Docling is imported lazily.

```bash
python -m venv .venv && source .venv/bin/activate
pip install fastapi 'uvicorn[standard]' python-multipart pandas openpyxl   # light: Excel + Markdown-error path
# pip install -r requirements.txt                                          # full: also installs Docling (torch, ~GB)
uvicorn app.main:app --reload --port 8080
curl -F file=@some.xlsx http://localhost:8080/convert
```

## Build & run with Docker

```bash
docker build -t hslu-aire-doc-service .
docker run -p 8080:8080 -e SERVICE_TOKEN=dev hslu-aire-doc-service
```

The image bakes the Docling models in, so the first request doesn't download them. It is large (Torch + models, ~2–4 GB) — expected.

## Deploy to AWS App Runner (from ECR)

Recommended instance: **1 vCPU / 4 GB** (Docling loads Torch + layout/table models). ~20–30 €/month always-on.

```bash
AWS_REGION=eu-central-1
ACCOUNT=302174038010                     # same account as the portal
REPO=hslu-aire-doc-service

# 1. ECR repo + login
aws ecr create-repository --repository-name $REPO --region $AWS_REGION || true
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com

# 2. Build (linux/amd64 for App Runner), tag, push
docker build --platform linux/amd64 -t $REPO .
docker tag $REPO $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO:latest
docker push $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO:latest
```

Then create the App Runner service (Console or CLI):
- **Source**: the ECR image above (private).
- **Port**: `8080`.
- **Health check**: HTTP path `/health`.
- **Instance**: 1 vCPU, 4 GB.
- **Env**: `SERVICE_TOKEN=<a long random secret>`.
- **Networking**: default (public ingress is fine; it's still token-guarded). For extra safety put it in a VPC and reach it via VPC connector from the portal.

Take note of the service's default URL (`https://xxxx.eu-central-1.awsapprunner.com`).

## Wire the portal to it

In `hslu-aire-server` (App Runner env):
- `DOC_SERVICE_URL=https://<this-service>.eu-central-1.awsapprunner.com`
- `DOC_SERVICE_TOKEN=<same secret as SERVICE_TOKEN>`

The portal proxies uploads from the "Dokument → Markdown" training block to `POST /convert` with that token.

## Notes / tuning

- **Cost**: always-on 4 GB ≈ 20–30 €/month; active compute for conversions is marginal on top.
- **Cold start**: none while always-on. (If you later move to Lambda, expect 15–30 s cold starts unless you use SnapStart.)
- **Pin Docling** (`requirements.txt`) before deploying so the model-prefetch line in the `Dockerfile` matches the installed version.
- **OCR** (scanned PDFs) is available in Docling but heavier; enable per-document via a Docling pipeline option if needed.
