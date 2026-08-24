# Repository Agent Instructions

## Repository Purpose

- This repository implements a secure, ACL-trimmed Retrieval-Augmented Generation (RAG)
  pipeline that ingests PDF documents from a SharePoint document library and serves
  grounded, per-document-permission-filtered answers.
- Primary technologies: Python 3.12, Azure Functions (Flex Consumption) with Durable
  Functions for ingestion, FastAPI + Uvicorn for retrieval, Azure Cosmos DB (NoSQL,
  vector + full-text), Microsoft Graph API, Azure OpenAI, Document Intelligence, Azure
  AI Language.
- Supported environments: Azure Container Apps for retrieval in dev; Azure Kubernetes
  Service (AKS) for retrieval in prod; Azure Functions Flex Consumption for ingestion in
  both. Infrastructure is provisioned via Bicep (`infra/`) through `azd provision`/`azd deploy`.

## Repository Map

- `app/function_app.py`: Durable Functions app — all HTTP endpoints (ingestion control,
  webhooks, query proxy), orchestrators, and activities. Single entry point for the
  ingestion Function App.
- `app/config.py`: environment-variable-backed configuration for ingestion (`IngestionConfig`).
- `app/ingestion/`: ingestion domain logic.
  - `services.py`: orchestration-facing business logic (`process_document`, `run_delta_sync`,
    `run_acl_resync_page`, `resync_document_acl`).
  - `repository.py`: schema-v1, full-sync-only Cosmos access. Deliberately forbids
    `patch_item`/`delta`-style terminology — enforced by
    `tests/ingestion/test_repository.py::test_repository_exposes_no_forbidden_api_or_terminology`.
  - `lifecycle_repository.py`: the *only* module allowed to issue Cosmos Patch/Delete
    calls and own the delta-sync cursor (retire, ACL refresh, hard-delete).
  - `graph.py` / `source_connector.py`: all Microsoft Graph and SharePoint REST access.
  - `extraction.py`, `chunking.py`, `enrichment.py`, `embedding.py`: per-stage pipeline logic.
  - `models.py`: schema-v1 domain records and validation.
- `app/retrieval/`: FastAPI retrieval service (deployed separately to ACA/AKS).
  - `main.py`: FastAPI app, `/api/query`, health probes, agentic/standard routing.
  - `service.py` (`RagService`): query planning, evidence retrieval, answer generation.
  - `cosmos.py` (`SecureCosmosRetriever`): the only module that builds/executes ACL-filtered
    Cosmos search queries.
  - `auth.py`: EasyAuth principal parsing and Graph transitive-group resolution.
- `infra/`: Bicep modules (source of truth for all Azure resources). `infra/main.json` is
  compiled output — do not edit manually.
- `tests/`: pytest suite, mirrors `app/` package structure (`tests/ingestion`,
  `tests/retrieval`, `tests/infra`, `tests/evaluation`, `tests/app_runtime`).
- `tools/`: ad hoc local developer/demo scripts against a *live deployed* environment
  (webhook/ACL walkthrough, retrieval query smoke test). Not part of the deployed
  application; currently untracked in git.
- `data/`: gitignored local exports from `/api/ingestion/inspect` for manual inspection —
  not committed source, do not treat as fixtures.
- `docs/`: see Documentation section below.

## Setup And Commands

Run commands from the repository root unless noted.

- Install: `python -m venv .venv` then `.venv/Scripts/activate` (Windows) then
  `pip install -r requirements-dev.txt` (installs `app/requirements.txt` plus `pytest`, `jsonschema`).
- Build: none — pure Python, no compile step. Container images for the retrieval service
  are built via `az acr build --registry <acr-name> --image retrieval-agent:<tag> --file retrieval/Dockerfile app/`.
- Focused tests: `python -m pytest tests/<area>/test_<file>.py -q` (add `::test_name` for a
  single test). Example verified this session:
  `python -m pytest tests/ingestion/test_lifecycle_repository.py tests/ingestion/test_incremental_sync.py -q`.
- Full tests: `python -m pytest tests/ --ignore=tests/infra/test_aks_contracts.py -q`
  (the ignored file requires a local `bicep` CLI executable not present in most dev
  environments; it is a known, pre-existing environment gap, not a code failure).
- Lint / Type check: **none configured**. `CONTRIBUTING.md` states "no additional linting
  rules," and `requirements-dev.txt` pins only `pytest` and `jsonschema` — do not invent a
  lint/type-check command; rely on the test suite and editor diagnostics.
- Run locally (ingestion): `cd app` then `func start` (requires `app/local.settings.json`
  with real Cosmos/Graph/Key Vault/OpenAI configuration — gitignored, never commit it).
- Run locally (retrieval): `cd app` then
  `python -m uvicorn retrieval.main:app --host 0.0.0.0 --port 8080` (requires the
  `RETRIEVAL_*`/`COSMOS_*`/`AZURE_OPENAI_*` environment variables from `RetrievalConfig`;
  cannot run meaningfully without a real Cosmos + Azure OpenAI backend).
- Deploy: `azd provision` then `azd deploy` (Function App); retrieval service is deployed
  separately via `az acr build` + `az containerapp update` (dev) or `kubectl`/AKS (prod) —
  see `README.md` "Deployment" and `docs/AZURE_SETUP.md`.

## Architecture Boundaries

- `app/ingestion/repository.py` owns full-sync writes; `app/ingestion/lifecycle_repository.py`
  owns every Patch/Delete Cosmos call and the delta-sync cursor. Do not add Patch/Delete
  calls to `repository.py` — this split is enforced by an explicit test.
- `app/retrieval/cosmos.py` (`SecureCosmosRetriever`) is the only module allowed to build
  ACL-filtered Cosmos search queries. New retrieval features must route through it, not a
  new direct Cosmos client.
- Dependency direction: `function_app.py` (HTTP/orchestration) → `ingestion/services.py`
  (business logic) → `ingestion/repository.py` / `lifecycle_repository.py` (data access) →
  Cosmos SDK. Retrieval mirrors this: `retrieval/main.py` → `retrieval/service.py` →
  `retrieval/cosmos.py` → Cosmos SDK. Do not skip layers.
- Access external systems only through their owning adapter: Microsoft Graph / SharePoint
  REST via `ingestion/graph.py` + `ingestion/source_connector.py`; Cosmos DB via the two
  repository modules above (ingestion) or `retrieval/cosmos.py`/`cosmos_registry.py`
  (retrieval); Azure OpenAI via the `_build_openai_client`-style factories in
  `function_app.py` / `retrieval/main.py`; Document Intelligence via `ingestion/extraction.py`;
  Azure AI Language via `ingestion/enrichment.py`.
- Preserve the schema-v1 contracts in `ingestion/models.py` (`SourceDocumentRecord`,
  `SearchChunkRecord`) and the `/api/query` request/response contract documented in
  `docs/API_REFERENCE.md` — both have dependent tests and dependent infra (Bicep indexing
  policies) that assume these shapes.

## Change Rules

- Follow existing patterns in the module you're changing before introducing a new one —
  e.g. new lifecycle operations belong in `lifecycle_repository.py` next to
  `retire_document`/`delete_document_and_chunks`, not scattered into `services.py`.
- Keep changes within the owning module unless a broader change is justified and stated.
- Do not manually edit `infra/main.json` (generated from `infra/main.bicep`) or files
  under `data/` (generated exports).
- Cosmos writes that mutate document lifecycle state must use ETag-guarded operations
  (`etag=`, `match_condition=MatchConditions.IfNotModified`) and map HTTP 412/404 to
  `LifecycleConflictError`, matching the existing pattern in `retire_document`,
  `refresh_document_acl`, and `delete_document_and_chunks`.
- Changing `RETIRED_REASONS`, chunk/document schema fields, or the `/api/query` request
  contract requires updating the corresponding Bicep indexing policy and/or
  `docs/API_REFERENCE.md` in the same change.

## Validation

- Run the narrowest relevant test file after the first substantive edit — see "Focused
  tests" above.
- Changes to `app/ingestion/lifecycle_repository.py` or delta-sync/ACL-resync logic in
  `app/ingestion/services.py` require `tests/ingestion/test_lifecycle_repository.py` and
  `tests/ingestion/test_incremental_sync.py`.
- Changes to `app/retrieval/cosmos.py` or `app/retrieval/service.py` require
  `tests/retrieval/test_cosmos.py` and `tests/retrieval/test_service.py`.
- Changes to `app/function_app.py` activities/orchestrators require the relevant
  `tests/ingestion/` suite plus, where practical, a focused test under
  `tests/app_runtime/` (monkeypatch the module-level `_build_*` functions rather than
  hitting real Azure — see `tests/app_runtime/test_retire_prior_version.py` for the pattern).
- Run `python -m pytest tests/ --ignore=tests/infra/test_aks_contracts.py -q` before
  declaring repository-wide validation complete.
- Never report a command as passing unless it was actually executed and its output observed.

## Security And Operations

- Never commit credentials, tokens, connection strings, certificates, or personal data.
  `app/local.settings.json`, `sharepoint-app-cert.cer`, and `sp-cert.pfx` are gitignored
  for this reason — keep them that way.
- All Azure service access uses Managed Identity (`DefaultAzureCredential`); Microsoft
  Graph/SharePoint access uses a certificate stored in Key Vault
  (`SHAREPOINT_CERTIFICATE_SECRET_NAME`). Do not introduce connection strings or API keys
  as an alternative.
- Preserve ACL enforcement in `app/retrieval/cosmos.py` (caller's transitive security
  groups ∩ document's `allowedGroupIds`) and the fail-closed ingestion ACL policy in
  `app/ingestion/graph.py` (sharing links and unverifiable groups must fail, never pass
  silently) — these are the tenant/authorization boundary for the whole system.
- All ingestion/query-audit events go through `write_audit_record()`
  (`ingestion/telemetry.py`) into the `service-audit` Cosmos container — do not bypass it
  with ad hoc logging for anything that should be auditable.

## Documentation

- `docs/ARCHITECTURE.md`: system diagrams, delta-sync/ACL-resync/webhook sequence
  diagrams, data model, retrieval routing — update when changing any of these flows.
- `docs/API_REFERENCE.md`: full HTTP contract for both the Function App and the
  retrieval service — update when changing request/response shapes or error codes.
- `docs/AZURE_SETUP.md`: step-by-step first-time Azure environment provisioning.
- `docs/DEMO_RUNBOOK.md`: PowerShell-based end-to-end validation script for
  ingestion/delta-sync/ACL-sync/retrieval — update when changing observable behavior it
  asserts on.
- `docs/TROUBLESHOOTING.md`: verified failure scenarios only — do not add speculative entries.
- `docs/PRODUCTION_READINESS.md`, `docs/AZURE_RESOURCES.md`: prod deployment checklist and
  exact resource SKUs/RBAC.
- No ADR directory exists today; record architecturally significant decisions as a new
  dated section in `docs/ARCHITECTURE.md` until one is introduced.
