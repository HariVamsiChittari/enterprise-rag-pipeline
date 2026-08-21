# Enterprise RAG Pipeline

Secure, ACL-trimmed RAG system that ingests PDFs from a SharePoint document library and serves grounded answers with per-document security trimming. Ingestion extracts, chunks, enriches, and embeds content into Cosmos DB. Retrieval uses a hybrid architecture: an LLM query planner routes simple queries through a fast standard RAG path (~5s target budget) and complex queries through an Agent Framework agentic path (~8-10s target budget), with automatic fallback. Figures are configured timeout budgets, not yet measured in production.

## Architecture

- **Ingestion:** Azure Functions (Flex Consumption) with Durable Functions orchestration
- **Retrieval:** Hybrid RAG (standard + agentic) on ACA (dev) / AKS (prod) with automatic routing
- **Durable Backend:** Durable Task Scheduler (singleton instance per source)
- **Storage:** Cosmos DB NoSQL (4 containers: ingestion-runs, source-documents, search-chunks, service-audit)
- **AI Services:** Document Intelligence, Azure AI Language, Azure OpenAI
- **Auth:** Managed Identity (Azure services) + Certificate credential (Microsoft Graph)
- **Networking:** VNet-integrated with Private Endpoints

For full Azure resource SKUs, RBAC roles, and network configuration, see [docs/AZURE_RESOURCES.md](docs/AZURE_RESOURCES.md).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed diagrams and data model.

## How It Works

**Ingestion (full-sync):**
```
POST /api/ingestion/full-sync → HTTP 202 + status polling URL
Orchestrator: activate → discover → [fan-out in waves] → finalize
Per-document: ACL verify → Download → Extract (DI) → Chunk → Enrich → Embed → Persist
```

**Incremental sync:** Microsoft Graph webhooks push change notifications in real-time. A daily reconciliation timer (04:00 UTC) runs delta queries as a safety net. Weekly ACL resync (Sunday 03:00 UTC) re-verifies permissions on all indexed documents.

**Retrieval:** `POST /api/query` → query planning → embed → ACL-filtered Cosmos search → LLM answer generation with `[S#]` citations.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed sequence diagrams.

## Configuration

All settings are environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `INGESTION_SOURCE_ID` | Yes | — | Stable source identifier |
| `SHAREPOINT_ASSIGNED_DRIVE_ID` | Yes | — | SharePoint drive to ingest |
| `SHAREPOINT_TENANT_ID` | Yes | — | Entra tenant ID |
| `SHAREPOINT_APP_CLIENT_ID` | Yes | — | Graph app registration client ID |
| `SHAREPOINT_CERTIFICATE_SECRET_NAME` | No | `sharepoint-app-cert` | Key Vault secret name for Graph PFX |
| `KEY_VAULT_URI` | Yes | — | Key Vault with Graph certificate |
| `COSMOS_ENDPOINT` | Yes | — | Cosmos DB endpoint |
| `COSMOS_DATABASE_NAME` | Yes | — | Database name |
| `OPENAI_ENDPOINT` | Yes | — | Azure OpenAI endpoint |
| `DOCUMENT_INTELLIGENCE_ENDPOINT` | Yes | — | DI endpoint (required if extraction enabled) |
| `AZURE_LANGUAGE_ENDPOINT` | Yes | — | Language service endpoint (required if any enrichment module enabled) |
| `AZURE_CLIENT_ID` | No | — | User-assigned Managed Identity client ID |
| `EXTRACTION_ENABLED` | No | `true` | Enable DI extraction |
| `KEY_PHRASES_ENABLED` | No | `true` | Enable Language AI key phrase extraction |
| `ENTITIES_ENABLED` | No | `true` | Enable Language AI named entity recognition |
| `SUMMARY_ENABLED` | No | `false` | Enable Language AI abstractive summary |
| `ALLOWED_FILE_EXTENSIONS` | No | `.pdf` | Comma-separated extensions |
| `WAVE_SIZE` | No | `4` | Parallel documents per wave |
| `WAVE_TIMEOUT_MINUTES` | No | `20` | Per-wave deadline before orchestrator moves on |
| `CHUNK_MAX_TOKENS` | No | `800` | Max tokens per chunk |
| `CHUNK_OVERLAP_TOKENS` | No | `100` | Token overlap between chunks |
| `EMBEDDING_BATCH_SIZE` | No | `100` | Texts per OpenAI embedding call |
| `MAX_PDF_PAGES` | No | `500` | Reject PDFs beyond this page count |
| `DOWNLOAD_TIMEOUT_SECONDS` | No | `120` | HTTP timeout for file download |
| `ACL_MAX_PAGES` | No | `10` | Max Graph paging calls for ACL check |
| `DELTA_MAX_PAGES` | No | `200` | Max Graph delta pages per sync tick |
| `DELTA_SYNC_SCHEDULE` | No | `0 0 4 * * *` | Daily reconciliation safety-net (NCRONTAB) |
| `ACL_RESYNC_SCHEDULE` | No | `0 0 3 * * 0` | ACL-resync timer (weekly Sunday 03:00 UTC) |
| `WEBHOOK_CLIENT_STATE` | Yes | — | Shared secret for Graph webhook clientState validation |
| `SUBSCRIPTION_RENEW_SCHEDULE` | No | `0 0 2 * * *` | Graph webhook subscription renewal (daily 02:00 UTC) |
| `FUNCTION_PUBLIC_BASE_URL` | Yes | — | Public HTTPS URL of Function App (for Graph webhook notification URL) |
| `SHAREPOINT_SITE_URL` | No | — | SharePoint site URL for site group ACL resolution via REST API |

### Retrieval Service (ACA / AKS)

| Variable | Required | Default | Description |
|---|---|---|---|
| `RETRIEVAL_SERVICE_URL` | Yes | — | Internal URL of retrieval service (Function App query proxy target) |
| `COSMOS_DATABASE` | Yes | — | Cosmos DB database name (note: ingestion uses `COSMOS_DATABASE_NAME`) |
| `AZURE_OPENAI_ENDPOINT` | Yes | — | Azure OpenAI endpoint (note: ingestion uses `OPENAI_ENDPOINT`) |
| `CHAT_DEPLOYMENT` | Yes | — | Chat completion model deployment name |
| `TENANT_ID` | Yes | — | Entra tenant ID for Graph group resolution |
| `MANAGED_IDENTITY_CLIENT_ID` | Yes | — | User-assigned MI client ID |
| `MAX_EVIDENCE_CHUNKS` | No | `5` | Default top-K chunks retrieved when `top_k` not in request |
| `INCLUDE_CITATIONS` | No | `true` | When `false`, response returns empty citations array |
| `ACL_ENABLED` | No | `true` | When `false`, skip ACL filtering — all authorized callers see all documents |
| `RETRIEVAL_TIMEOUT_SECONDS` | No | `5.0` | Cosmos retrieval timeout per query |
| `GENERATION_TIMEOUT_SECONDS` | No | `15.0` | Answer generation LLM call timeout |
| `AGENT_TIMEOUT_SECONDS` | No | `8.0` | Agentic path timeout before fallback |
| `AGENT_MAX_ITERATIONS` | No | `5` | Max LLM reasoning roundtrips per agentic request |
| `RATE_LIMIT_RPM` | No | `30` | Per-user requests per minute **per replica** before HTTP 429 (effective ceiling ≈ value × replica count; not a hard DoS control at scale-out — use Front Door WAF for enforcement) |
| `QUERY_PROXY_TIMEOUT_SECONDS` | No | `30.0` | Function App → retrieval service proxy timeout |

## Endpoints

| Route | Method | Purpose |
|---|---|---|
| `/api/ingestion/full-sync` | POST | Start ingestion (returns 202, 409 if running) |
| `/api/ingestion/status` | GET | Query orchestration status |
| `/api/ingestion/terminate` | POST | Terminate orchestration, force-fail stuck docs, finalize run |
| `/api/ingestion/retry-failed` | POST | Retry only the failed docs from the current full-sync run |
| `/api/ingestion/purge` | DELETE | Delete items from a Cosmos container (targeted IDs or purge-all) |
| `/api/ingestion/inspect` | GET | Read Cosmos data (container, runId, limit) |
| `/api/query` | POST | Proxy RAG queries to the retrieval service |
| `/api/webhook/sharepoint` | POST | Microsoft Graph change notifications (no auth required) |
| `/api/webhook/lifecycle` | POST | Graph subscription lifecycle events |

Retrieval is served by the hybrid RAG service on ACA / AKS (Function App proxies `/api/query` via `RETRIEVAL_SERVICE_URL`).

> **See [docs/API_REFERENCE.md](docs/API_REFERENCE.md)** for the complete API contract: headers, request/response schemas, error codes, and PowerShell examples for every endpoint.

## Query API

**Request:** `POST /api/query`

```http
POST /api/query HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json

{
  "question": "What is the password policy?",
  "mode": "hybrid",
  "history": [],
  "top_k": 5
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `question` | string | Yes | User question (1–4000 chars) |
| `mode` | string | No | `hybrid` (default), `vector`, `full_text` |
| `history` | array | No | Conversation history for multi-turn |
| `top_k` | int | No | Number of chunks to retrieve (1–20, default: `MAX_EVIDENCE_CHUNKS`) |

**Response:**

```json
{
  "answer": "The policy requires MFA [S1]...",
  "citations": [
    { "ref": "[S1]", "source_name": "Policy.pdf", "url": "https://...sharepoint.com/.../Policy.pdf#page=3" }
  ],
  "request_id": "uuid"
}
```

For error codes, rate limiting, and multi-turn history format, see [docs/API_REFERENCE.md](docs/API_REFERENCE.md#post-apiquery).

## Idempotent Re-Runs

Full-sync skips unchanged files automatically (same eTag). Use `retry-failed` for failed documents instead of re-running full-sync. Delta-sync retries failures automatically on the next timer tick. See [ARCHITECTURE.md](docs/ARCHITECTURE.md#idempotent-re-runs-skip-if-ready) for details.

## Security

- Files without verified Entra security groups are rejected (fail-closed)
- Only groups with `securityEnabled=true` are accepted
- Retrieval requires caller's group membership to match document ACLs
- All Azure access via Managed Identity (no secrets in code)
- Graph access via certificate stored in Key Vault
- Prompt injection defense: hardened system prompt + chunk sanitization (strips injection prefixes) + input segmentation
- Per-user rate limiting **per replica** (default 30 RPM, configurable via `RATE_LIMIT_RPM`; effective ceiling ≈ value × replica count — not a hard DoS control at scale-out)
- Thread-safe concurrent retrieval with `threading.Lock` on shared state
- EasyAuth with Entra ID: tenant-locked, audience-validated; production should set `allowedApplications` to restrict calling clients
- **Known gap**: `allowedApplications` restricts which client apps can call the API, but the Function App does not currently enforce per-user roles on admin/destructive endpoints (`purge`, `terminate`, `retry-failed`, `inspect`) — any caller from an allowed application can invoke them. See `docs/ARCHITECTURE.md` Known Limitations.
- AKS pods: Pod Security Standards (Restricted) — `runAsNonRoot`, `readOnlyRootFilesystem`, `drop ALL` capabilities
- OpenAI clients: `max_retries=2` for transient 429/5xx resilience

## Initial Deployment (Bootstrap)

On first deploy to an empty environment, the ACA uses `mcr.microsoft.com/k8se/quickstart:latest` as a placeholder image (ACR is empty). After infra provisioning, build and push the real image:

```bash
# 1. Deploy infrastructure (ACA starts with MCR placeholder)
az deployment group create --resource-group <rg> --template-file infra/main.bicep --parameters infra/main.parameters.dev.bicepparam

# 2. Build and push real image
az acr build --registry <acr-name> --image retrieval-agent:v1 --file retrieval/Dockerfile app/

# 3. Update ACA to real image
az containerapp update --name <aca-name> --resource-group <rg> --image <acr>.azurecr.io/retrieval-agent:v1

# 4. Deploy Function App code
func azure functionapp publish <func-app-name> --python
```

## Local Development

```bash
python -m venv .venv
.venv/Scripts/activate      # Windows
pip install -r requirements-dev.txt
python -m pytest tests/ --ignore=tests/infra/test_aks_contracts.py -q
```

## Deployment

```bash
# Infrastructure + Function App
azd provision
azd deploy

# Retrieval service (ACA)
cd app
az acr build --registry <acr-name> --image retrieval-agent:latest --file retrieval/Dockerfile .
az containerapp update --name <aca-name> --resource-group <rg> --image <acr>.azurecr.io/retrieval-agent:latest
```

## Project Structure

```
├── azure.yaml             # azd service definitions
├── pyproject.toml         # Python project metadata
├── requirements-dev.txt   # Dev dependencies (pytest, jsonschema)
├── app/
│   ├── function_app.py    # DFApp: endpoints + orchestrators + activities
│   ├── config.py          # Environment configuration
│   ├── host.json          # Function timeout (30 min)
│   ├── requirements.txt   # Runtime dependencies
│   ├── ingestion/
│   │   ├── models.py      # Domain models + schema v1 contracts
│   │   ├── services.py    # Business logic: activate, discover, process, finalize, delta-sync, ACL resync
│   │   ├── repository.py  # Cosmos persistence + retry
│   │   ├── lifecycle_repository.py  # Document lifecycle: retire, ACL refresh, delta cursor
│   │   ├── source_connector.py      # SharePoint connector protocol + implementation
│   │   ├── graph.py       # Microsoft Graph: discovery, ACL, download, delta
│   │   ├── extraction.py  # Document Intelligence
│   │   ├── chunking.py    # Token-based page-aware chunking
│   │   ├── enrichment.py  # Language AI: key phrases, entities, summary
│   │   ├── embedding.py   # OpenAI embedding
│   │   ├── telemetry.py   # Ingestion audit to Cosmos
│   │   ├── subscription.py # Graph webhook subscription lifecycle
│   │   └── errors.py      # TerminalDocumentError, StaleFenceError
│   └── retrieval/
│       ├── agent.py       # Agent Framework agent factory
│       ├── auth.py        # EasyAuth + GraphGroupResolver
│       ├── config.py      # Retrieval configuration
│       ├── cosmos.py      # SecureCosmosRetriever (ACL-filtered queries)
│       ├── cosmos_registry.py # Multi-instance Cosmos fan-out
│       ├── main.py        # FastAPI app with hybrid routing
│       ├── service.py     # RagService (plan → sanitize → retrieve → generate)
│       ├── tools.py       # Agent Framework search tool (multi-instance)
│       ├── telemetry.py   # LLM audit to Cosmos
│       ├── Dockerfile     # Retrieval container image
│       └── kubernetes/    # AKS/ACA deployment manifests
├── infra/                 # Bicep modules (Cosmos, Functions, VNet, PEs)
├── evaluation/            # Evaluation schemas (ground-truth, experiments)
├── tests/                 # Unit tests (ingestion, retrieval, infra)
├── docs/                  # ARCHITECTURE.md, API_REFERENCE.md, AZURE_RESOURCES.md, AZURE_SETUP.md, PRODUCTION_READINESS.md, DEMO_RUNBOOK.md, INFRASTRUCTURE_REQUEST.md, archive/
└── data/                  # Sample Cosmos data exports
```
