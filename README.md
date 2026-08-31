# Enterprise RAG Pipeline

Secure, ACL-trimmed RAG system that ingests PDFs from one SharePoint document library and serves grounded answers with per-document security trimming. Ingestion extracts, chunks, enriches, and embeds content into Cosmos DB. Retrieval uses an LLM query planner to route one planned query through the standard path and two or more planned queries through an Agent Framework path with automatic fallback.

## Architecture

- **Ingestion:** Azure Functions (Flex Consumption) with Durable Functions orchestration
- **Retrieval:** Hybrid RAG (standard + agentic) on Azure Container Apps with automatic routing
- **Durable Backend:** Durable Task Scheduler (fresh instance ID per run, tracked via Cosmos)
- **Storage:** Cosmos DB NoSQL (ingestion-runs, source-documents, search-chunks, retrieval-config, service-audit)
- **AI Services:** Document Intelligence, Azure AI Language, Azure OpenAI
- **Auth:** Managed Identity (Azure services) + Certificate credential (Microsoft Graph)
- **Networking:** VNet-integrated with Private Endpoints

For full Azure resource SKUs, RBAC roles, and network configuration, see [docs/AZURE_RESOURCES.md](docs/AZURE_RESOURCES.md).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed diagrams and data model.

## How It Works

**Ingestion (full-sync):**

```text
POST /api/ingestion/full-sync → HTTP 202 + status polling URL
Orchestrator: activate → discover → [fan-out in waves] → finalize
Per-document: ACL verify → Download → Extract → Chunk → Enrich → Embed → write ineligible chunks → admit generation → READY
```

**Incremental sync:** Microsoft Graph webhooks push change notifications. A daily reconciliation timer runs delta queries as a safety net, weekly ACL resync re-verifies permissions, and a 10-minute lifecycle reconciliation repairs interrupted transitions, duplicate ready versions, and orphan chunks.

**Retrieval:** `POST /api/query` → query planning → embed → ACL-filtered Cosmos search → LLM answer generation with `[S#]` citations.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed sequence diagrams.

## Configuration

The complete ingestion, Function gateway, retrieval, operations-job, and deployment inventory is [docs/CONFIGURATION.md](docs/CONFIGURATION.md). The tables below summarize the most common runtime settings.

| Variable | Required | Default | Description |
|---|---|---|---|
| `INGESTION_SOURCE_ID` | Yes | — | Stable source identifier |
| `SHAREPOINT_ASSIGNED_DRIVE_ID` | Yes | — | SharePoint drive to ingest |
| `SHAREPOINT_SITE_URL` | Yes | — | HTTPS SharePoint site URL used to bind the configured drive and expand site-group membership |
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

### Retrieval Service (Azure Container Apps)

| Variable | Required | Default | Description |
|---|---|---|---|
| `RETRIEVAL_SERVICE_URL` | Yes | — | Internal URL of retrieval service (Function App query proxy target) |
| `COSMOS_DATABASE` | Yes | — | Cosmos DB database name (note: ingestion uses `COSMOS_DATABASE_NAME`) |
| `AZURE_OPENAI_ENDPOINT` | Yes | — | Azure OpenAI endpoint (note: ingestion uses `OPENAI_ENDPOINT`) |
| `CHAT_DEPLOYMENT` | Yes | — | Chat completion model deployment name |
| `TENANT_ID` | Yes | — | Entra tenant ID for Graph group resolution |
| `MANAGED_IDENTITY_CLIENT_ID` | Yes | — | User-assigned MI client ID |
| `RETRIEVAL_API_AUDIENCE` | Yes | — | Exact v2 access-token audience accepted by retrieval |
| `RETRIEVAL_GATEWAY_CLIENT_ID` | Yes | — | Function UAMI application/client ID |
| `RETRIEVAL_GATEWAY_PRINCIPAL_ID` | Yes | — | Function UAMI service-principal object ID |
| `DEPLOYMENT_INSTANCE_ID` | Yes | — | Retrieval catalog partition key |
| `RETRIEVAL_CATALOG_DIGEST` | Yes | — | Immutable `sha256:<digest>` selecting the startup catalog |
| `MAX_EVIDENCE_CHUNKS` | No | `5` | Default top-K chunks retrieved when `top_k` not in request |
| `INCLUDE_CITATIONS` | No | `true` | When `false`, response returns empty citations array |
| `ACL_ENABLED` | No | `true` | When `false`, skip ACL filtering — all authorized callers see all documents |
| `RETRIEVAL_TIMEOUT_SECONDS` | No | `5.0` | Retrieval fan-out wait bound and query-embedding timeout |
| `GENERATION_TIMEOUT_SECONDS` | No | `15.0` | Answer generation LLM call timeout |
| `AGENT_TIMEOUT_SECONDS` | No | `20.0` in Bicep | Agentic path deadline before fallback |
| `RETRIEVAL_OPERATION_TIMEOUT_SECONDS` | No | `27.0` | ACA wall-clock deadline for `/api/query` |
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
| `/api/ingestion/inspect` | GET | Read a bounded Cosmos sample; `runId` applies only to `source-documents` |
| `/api/query` | POST | Proxy RAG queries to the retrieval service |
| `/api/webhook/sharepoint` | POST | Microsoft Graph change notifications (no auth required) |
| `/api/webhook/lifecycle` | POST | Graph subscription lifecycle events |

Retrieval is served by Azure Container Apps. The Function validates delegated user claims, then calls retrieval with its managed-identity service token, a bounded gateway context, and a Function-owned request ID.

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
  "top_k": 5,
  "scoring_profile": "hr-relevance",
  "expand_synonyms": true
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `question` | string | Yes | User question (1–4000 chars) |
| `mode` | string | No | `hybrid` (default), `vector`, `full_text` |
| `history` | array | No | Conversation history for multi-turn |
| `top_k` | int | No | Number of chunks to retrieve (1–20, default: `MAX_EVIDENCE_CHUNKS`) |
| `scoring_profile` | string | No | Profile from the pinned retrieval catalog; omitted uses the catalog default |
| `expand_synonyms` | boolean | No | `false` disables expansion; `true` or omitted uses the selected profile's map when catalog synonyms are enabled |

Freshness is not a request field. It is applied automatically when the selected scoring profile declares freshness functions over `sourceModifiedAt`.

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

Full sync skips a prior-ready file when its source eTag and available modification timestamp are unchanged. Use `retry-failed` for failed documents instead of re-running full sync. Delta failures retain the previous cursor and are replayed by a later tick. See [ARCHITECTURE.md](docs/ARCHITECTURE.md#idempotent-re-runs-skip-if-ready) for details.

## Security

- Files without verified Entra security groups are rejected (fail-closed)
- Only groups with `securityEnabled=true` are accepted
- Retrieval requires caller's group membership to match document ACLs
- Azure service access uses Managed Identity; the SharePoint certificate is loaded from Key Vault and webhook `clientState` is a secure app setting
- Graph access via certificate stored in Key Vault
- Prompt injection defense: hardened system prompt + chunk sanitization (strips injection prefixes) + input segmentation
- Per-user rate limiting **per replica** (default 30 RPM, configurable via `RATE_LIMIT_RPM`; effective ceiling ≈ value × replica count — not a hard DoS control at scale-out)
- Thread-safe concurrent retrieval with `threading.Lock` on shared state
- EasyAuth requires the exact Function audience and configured caller application list in every environment
- **Known gap**: `allowedApplications` restricts which client apps can call the API, but the Function App does not currently enforce per-user roles on admin/destructive endpoints (`purge`, `terminate`, `retry-failed`, `inspect`) — any caller from an allowed application can invoke them. See `docs/ARCHITECTURE.md` Known Limitations.
- OpenAI clients: `max_retries=2` for transient 429/5xx resilience

## Initial Deployment

Deployment is controlled by [scripts/deploy.ps1](scripts/deploy.ps1). It validates reviewed plan/source hashes and runs `Authority`, `Foundation`, `Build`, `Operations`, `Catalog`, `CatalogVerify`, `Final`, and `Function`; after E2E validation and explicit approval, `OperationsCleanup` removes the temporary publisher job. Serving phases require an immutable image reference (`repository@sha256:<digest>`) and immutable catalog digest. See [docs/AZURE_SETUP.md](docs/AZURE_SETUP.md) for prerequisites and commands.

## Local Development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest tests/ --ignore=tests/infra/test_aks_contracts.py -q
```

## Deployment

Use the guarded phases in `scripts/deploy.ps1`; do not deploy mutable image tags or update ACA directly. Preview is the default, and Azure mutation requires `-Execute` plus the reviewed hashes and target identifiers.

## Project Structure

```text
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
│       └── kubernetes/    # Retained inactive AKS manifest
├── infra/                 # Bicep modules (Cosmos, Functions, VNet, PEs)
├── evaluation/            # Evaluation schemas (ground-truth, experiments)
├── tests/                 # Unit tests (ingestion, retrieval, infra)
├── docs/                  # Architecture, API, configuration, setup, readiness, demo, and infrastructure guides
└── data/                  # Sample Cosmos data exports
```
