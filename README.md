# SharePoint PDF RAG — Ingestion Pipeline

Secure, ACL-trimmed RAG system that ingests PDFs from a SharePoint document library, extracts and chunks content, generates embeddings, and stores vectors in Cosmos DB. Retrieval is served by a Microsoft Agent Framework (MAF) agent on AKS that queries Cosmos directly with ACL filtering.

## Architecture

- **Ingestion:** Azure Functions (Flex Consumption) with Durable Functions orchestration
- **Retrieval:** MAF agent on AKS querying Cosmos DB directly
- **Durable Backend:** Durable Task Scheduler (singleton instance per source)
- **Storage:** Cosmos DB NoSQL (3 containers: ingestion-runs, source-documents, search-chunks)
- **AI Services:** Document Intelligence, Azure AI Language, Azure OpenAI
- **Auth:** Managed Identity (Azure services) + Certificate credential (Microsoft Graph)
- **Networking:** VNet-integrated with Private Endpoints

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed diagrams and data model.

## How It Works

```
POST /api/ingestion/full-sync → HTTP 202 + status polling URL

Orchestrator: activate → discover → [fan-out process in waves] → finalize

Per-document activity:
  ACL verify → Download PDF → Extract (DI) → Chunk → Clean → Enrich → Embed → Persist
```

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

## Endpoints (Function App — Ingestion only)

| Route | Method | Purpose |
|---|---|---|
| `/api/ingestion/full-sync` | POST | Start ingestion (returns 202, 409 if running) |
| `/api/ingestion/status` | GET | Query orchestration status |
| `/api/ingestion/terminate` | POST | Terminate a stuck orchestration |
| `/api/ingestion/inspect` | GET | Read Cosmos data (container, runId, limit) |

Retrieval is served by the MAF agent on AKS (not the Function App).

## Idempotent Re-Runs

Full-sync is safe to re-run. Files already `ready` with unchanged content (same eTag) are automatically skipped. Only new, modified, or previously-failed files are reprocessed. This makes recovery from transient failures fast and cheap.

## Security

- Files without verified Entra security groups are rejected (fail-closed)
- Only groups with `securityEnabled=true` are accepted
- Retrieval requires caller's group membership to match document ACLs
- All Azure access via Managed Identity (no secrets in code)
- Graph access via certificate stored in Key Vault

## Local Development

```bash
cd src/customer-solutions/rag-project
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements-dev.txt
pytest  # 99 tests expected
```

## Deployment

```bash
cd src/customer-solutions/rag-project
azd provision   # Create/update infrastructure (Cosmos, Functions, VNet, PEs)
azd deploy      # Deploy function app code
```

## Project Structure

```
├── azure.yaml             # azd service definitions
├── pyproject.toml         # Python project metadata
├── requirements-dev.txt   # Dev dependencies (pytest, jsonschema)
├── app/
│   ├── function_app.py    # DFApp: endpoints + orchestrator + activities
│   ├── config.py          # Environment configuration
│   ├── host.json          # Function timeout (30 min)
│   ├── requirements.txt   # Runtime dependencies
│   ├── ingestion/
│   │   ├── models.py      # Domain models + schema v1 contracts
│   │   ├── services.py    # Business logic: activate, discover, process, finalize
│   │   ├── repository.py  # Cosmos persistence + retry
│   │   ├── graph.py       # Microsoft Graph: discovery, ACL, download
│   │   ├── extraction.py  # Document Intelligence
│   │   ├── chunking.py    # Token-based page-aware chunking
│   │   ├── enrichment.py  # Language AI: key phrases, entities, summary
│   │   ├── embedding.py   # OpenAI embedding
│   │   └── errors.py      # TerminalDocumentError, StaleFenceError
│   └── retrieval/
│       ├── auth.py        # EasyAuth + GraphGroupResolver
│       ├── cosmos.py      # SecureCosmosRetriever (ACL-filtered queries)
│       ├── service.py     # RagService (embed → retrieve → chat)
│       └── kubernetes/    # AKS deployment manifests
├── infra/                 # Bicep modules (Cosmos, Functions, VNet, PEs)
├── scripts/               # Operational scripts (preflight, ACL sync, validation)
├── tests/                 # Unit tests (ingestion, retrieval, infra)
├── docs/                  # ARCHITECTURE.md, PRODUCTION_READINESS.md
└── data/                  # Sample Cosmos data exports
```
