# Azure Resource, Identity, and Network Inventory

This document describes the resources created or consumed by the active Bicep deployment. It does not claim that a particular Azure environment currently matches the template; use Azure inventory tools for live-state verification.

## Deployment Boundary

- Active serving topology: Azure Functions Flex Consumption plus Azure Container Apps.
- AKS modules and manifests remain in the repository but are not referenced by `infra/main.bicep`.
- The resource group, Azure OpenAI account, SharePoint certificate Key Vault, and three Entra application registrations are external prerequisites.
- `scripts/deploy.ps1` is the deployment authority. Serving resources are conditional on `deployServing`; the temporary catalog publisher job is conditional on `deployOperations`.

## Resources

| Resource | Active module | Purpose and material settings |
| --- | --- | --- |
| Application Insights and Log Analytics | `monitoring.bicep` | Function/retrieval telemetry and ACA logs; optional daily cap |
| Storage account | `storage.bicep` | Functions deployment and runtime storage; ZRS default, shared-key access disabled, public access disabled |
| Function UAMI | `identity.bicep` | Function runtime identity and retrieval gateway identity |
| Cosmos DB NoSQL account | `cosmos.bicep` | Strong consistency, single region, local auth disabled, public access disabled, vector/full-text capabilities |
| Durable Task Scheduler | `durable-task.bicep` | Durable Functions orchestration backend and source-derived task hub |
| Document Intelligence | `ai-services.bicep` | PDF layout extraction; public access disabled |
| Azure AI Language | `ai-services.bicep` | Key phrases, entities, and optional summaries; public access disabled |
| Virtual network, private DNS, private endpoints | `networking.bicep` | Function integration, ACA infrastructure, and private endpoint subnets |
| ACA managed environment | `aca-environment.bicep` | Internal VNet-integrated environment with Log Analytics |
| Azure Container Registry | `acr.bicep` | Retrieval image storage; serving uses an immutable digest |
| Retrieval UAMI | `identity.bicep` | ACA access to Cosmos, Azure OpenAI, Graph, and ACR |
| Operations UAMI | `identity.bicep` | Temporary private catalog-publisher job |
| Function App | `functions.bicep` | Python 3.12 Flex Consumption API and Durable activities; created in `Final` |
| Retrieval Container App | `aca.bicep` | FastAPI service, single active revision, authenticated Function-only ingress; created in `Final` |
| Temporary catalog job | `aca-operations-job.bicep` | Publishes the reviewed immutable catalog; removed by `OperationsCleanup` |

## Cosmos DB Containers

| Container | Partition key | Purpose |
| --- | --- | --- |
| `ingestion-runs` | `/sourceId` | Full-sync runs, source controls, delta cursor, trigger IDs, webhook subscription ID |
| `source-documents` | `/sourceRunId` | Source manifests, lifecycle state, ACL, source timestamps, chunk counts |
| `search-chunks` | `/documentKey` | Content, 3,072-dimensional embeddings, full-text fields, ACL and retrieval eligibility |
| `retrieval-config` | `/deploymentInstanceId` | Immutable `catalog:<sha256>` items and publication pointer metadata |
| `service-audit` | `/id` | Best-effort service/lifecycle audit records with a 90-day TTL |

`search-chunks` has DiskANN on `/embedding`, full-text indexes on `/content` and `/searchableText`, and indexes for ACL, source timestamp, retrieval eligibility, and lifecycle generation.

## Managed Identities and RBAC

### Function UAMI

Active Bicep creates seven core assignments plus three cross-module assignments:

- Cosmos DB Built-in Data Contributor at account scope.
- Storage Blob Data Owner.
- Storage Queue Data Contributor.
- Storage Table Data Contributor.
- Cognitive Services User on Document Intelligence.
- Cognitive Services User on Azure AI Language.
- Monitoring Metrics Publisher on Application Insights.
- Key Vault Secrets User on the existing SharePoint certificate vault.
- Cognitive Services OpenAI User on the existing Azure OpenAI account.
- Durable Task Data Contributor on the scheduler/task hub.

The Function UAMI is also the application identity allowed to call retrieval. Assignment of the external retrieval API's `Retrieval.Gateway` app role is an Entra prerequisite; active Bicep does not mutate directory objects.

### Retrieval UAMI

- Cosmos DB Built-in Data Reader scoped separately to `search-chunks`, `source-documents`, and `retrieval-config`.
- Cosmos DB Built-in Data Contributor scoped to `service-audit`.
- Cognitive Services OpenAI User on the existing Azure OpenAI account.
- ACR pull access.
- Microsoft Graph application permissions required for transitive group resolution are external Entra prerequisites.

### Operations UAMI

- ACR pull access.
- Cosmos DB Built-in Data Contributor scoped to `retrieval-config`.
- Used only by the temporary manual catalog-publisher job.

## External Identity Prerequisites

| Registration | Required contract |
| --- | --- |
| SharePoint ingestion application | Certificate credential; Graph application permissions for site/file/group reads and SharePoint `Sites.Read.All` for site-group expansion |
| Function API application | Exposes delegated `user_impersonation`; exact API audience is configured in EasyAuth |
| Retrieval API application | Exposes application role `Retrieval.Gateway`; Function UAMI receives that role |

`FUNCTION_ALLOWED_CALLER_CLIENT_ID` is required. Function EasyAuth accepts exactly `FUNCTION_API_AUDIENCE` and the configured caller application. ACA Authentication accepts only the Function UAMI application and principal.

## Network Topology

The VNet contains three subnets:

- Function integration subnet.
- Private endpoint subnet.
- ACA environment infrastructure subnet.

Seven private endpoints are created for Storage blob, queue, and table; Cosmos SQL; the existing Key Vault; Document Intelligence; and Azure AI Language. Six unique private DNS zones are linked because both AI services share the Cognitive Services zone.

Cosmos, Storage, Document Intelligence, and Language disable public access. The Key Vault is externally supplied: this deployment creates its private endpoint and role assignment but does not change the vault's existing public-access policy. ACR, monitoring endpoints, Microsoft Graph, and the externally supplied Azure OpenAI network policy are outside that private-endpoint claim.

## Deployment Outputs

`infra/main.bicep` returns:

- Function name and URL.
- Function, retrieval, and operations UAMI client/principal IDs.
- Operations job name when deployed.
- Retrieval API service-principal ID passed as an external validation input.
- Existing Key Vault name.
- Cosmos endpoint and database name.
- Document Intelligence and Azure OpenAI endpoints.
- ACR login server.
- Retrieval URL and Container App name when serving is deployed.
- Retrieval configuration map.

## Sources of Truth

- Resource graph: `infra/main.bicep` and `infra/modules/*.bicep`.
- Deployment sequencing and mutation controls: `scripts/deploy.ps1`.
- Parameters: `infra/main.parameters.bicepparam` and `infra/operations.parameters.bicepparam`.
- Contract tests: `tests/infra/test_bicep_contracts.py` and `tests/infra/test_deployment_contract.py`.
- Runtime boundaries: [ARCHITECTURE.md](ARCHITECTURE.md).
