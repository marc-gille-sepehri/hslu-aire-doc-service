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

  Optional form fields (both default to today's behaviour, so existing callers are unaffected):

  | field | values | default | meaning |
  |---|---|---|---|
  | `outputFormat` | `markdown` \| `cells` \| `both` | `markdown` | add the cell-addressed serialization |
  | `formulaMode` | `silent` \| `error` \| `formula` | `silent` | how formula cells are rendered in `cells` |

  With `cells` or `both`, the response gains a `cells` object (`text`, `sheets`, `formulaMode`,
  `truncated`, `warnings`). For an input without table structure — PDF, DOCX, `.xls` — it is
  `{ "applicable": false, "message": "Zellenformat nicht anwendbar …" }` with HTTP 200; the
  conversion is never failed over an inapplicable format request.

  ```bash
  curl -F file=@objektliste.xlsx -F outputFormat=cells -F formulaMode=formula localhost:8080/convert
  ```

  Format and rationale: [`docs/delta-spec-doc-convert-cells.md`](docs/delta-spec-doc-convert-cells.md).

  > `excel.serialized` is the **older** row-wise format (0-based numeric row/column indices) and
  > is deprecated in favour of `excel.cells`. It is still emitted; remove only after auditing
  > consumers in `hslu-aire-server`.

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

Tests (spreadsheet paths only — no Docling needed):

```bash
pip install pytest httpx
python -m pytest tests/ -q
python tests/make_fixtures.py    # only after changing a fixture
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
- **Autoscaling**: **do not leave this on `DefaultConfiguration`** — see below.

Take note of the service's default URL (`https://xxxx.eu-central-1.awsapprunner.com`).

### Autoscaling: cap the concurrency

App Runner's default sends up to **100 concurrent requests to a single container**. That is
wrong for this service: `/convert` reads the whole upload into memory (`await file.read()`), so
100 in-flight requests are ~3 GB of buffers at the 30 MB limit — before any parsing — on a 4 GB
instance that also holds Torch.

The service uses `doc-service-lowconc` (MaxConcurrency 2, Min 1, Max 10): a third simultaneous
request starts a *second instance* instead of sharing the first one's memory. Idle cost is
unchanged — only `MinSize` instances are billed at rest.

```bash
aws apprunner create-auto-scaling-configuration \
  --auto-scaling-configuration-name doc-service-lowconc \
  --max-concurrency 2 --min-size 1 --max-size 10 --region eu-central-1

aws apprunner update-service --region eu-central-1 \
  --service-arn <service-arn> --auto-scaling-configuration-arn <arn-from-above>
```

Autoscaling configurations are immutable — changing the values means creating a new revision and
re-attaching it (which redeploys the service). This service is **not** Terraform-managed; only
`hslu-aire-server` is.

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
