# Cloud Ops Request — Enterprise RAG Pipeline

Submit this as a single cloud ops ticket. Replace all `<placeholder>` values before submission.

---

## Section 1: Resource Group

| Item | Value |
|---|---|
| Resource Group Name | `rg-rag-<env>` |
| Location | `<region>` (e.g., `eastus2`) |

---

## Section 2: User-Assigned Managed Identities

| # | Identity Name | Purpose |
|---|---|---|
| MI-1 | `rag-<env>-functions-mi` | Function App ingestion pipeline |
| MI-2 | `rag-<env>-aks-retrieval-mi` | AKS retrieval service (with Workload Identity federation) |

**MI-2 Federated Credential:**

| Property | Value |
|---|---|
| Issuer | AKS OIDC issuer URL |
| Subject | `system:serviceaccount:default:retrieval-agent-sa` |
| Audience | `api://AzureADTokenExchange` |

---

## Section 3: Azure Resources

| # | Resource Type | Name Pattern | SKU/Tier | Notes |
|---|---|---|---|---|
| R-1 | Azure Functions (Flex Consumption) | `rag-<env>-func-<suffix>` | FC1 | Python 3.12, VNet-integrated |
| R-2 | Cosmos DB NoSQL | `rag-<env>-cosmos-<suffix>` | Serverless | 4 containers (see below) |
| R-3 | Storage Account | `strag<env><suffix>` | Standard LRS | Blob, Queue, Table |
| R-4 | Key Vault | `rag-<env>-kv-<suffix>` | Standard | RBAC access model |
| R-5 | Container Registry | `rag<env>acr<suffix>` | Basic | Retrieval container image |
| R-6 | Document Intelligence | `rag-<env>-doc-intel-<suffix>` | F0 (dev) / S0 (prod) | prebuilt-layout model |
| R-7 | AI Language | `rag-<env>-lang-<suffix>` | F0 (dev) / S0 (prod) | Key phrases, entities |
| R-8 | Azure OpenAI | *existing or new* | S0 | 2 model deployments |
| R-9 | Application Insights | `rag-<env>-ai` | — | + Log Analytics workspace |
| R-10 | Virtual Network | `rag-<env>-vnet` | — | 3 subnets (see networking) |
| R-11 | Durable Task Scheduler | `rag-<env>-dts-<suffix>` | — | Orchestration state |

### Cosmos DB Containers (within R-2)

| Container | Partition Key | Special Indexes |
|---|---|---|
| `ingestion-runs` | `/sourceId` | Standard |
| `source-documents` | `/sourceRunId` | Standard |
| `search-chunks` | `/documentKey` | DiskANN vector index (`/embedding`, float32, 3072 dims, cosine), full-text index (`/content`, `/searchableText`) |
| `service-audit` | `/id` | Standard |

### Azure OpenAI Deployments (within R-8)

| Deployment Name | Model | Min Capacity |
|---|---|---|
| `text-embedding-3-large` | text-embedding-3-large | 120K TPM |
| `<chat-deployment>` | gpt-4o / gpt-4o-mini | 80K TPM |

---

## Section 4: Networking

**VNet Address Space:** `10.20.0.0/22`

| Subnet | CIDR | Delegation | Purpose |
|---|---|---|---|
| `function-integration` | `10.20.0.0/27` | `Microsoft.App/environments` | Function App VNet integration |
| `private-endpoints` | `10.20.0.32/27` | None | Private endpoints |
| `aca-environment` | `10.20.2.0/23` | `Microsoft.App/environments` | ACA or AKS peering |

### Private Endpoints (7)

| # | Target Resource | Group ID | Private DNS Zone |
|---|---|---|---|
| PE-1 | Storage Account (blob) | `blob` | `privatelink.blob.core.windows.net` |
| PE-2 | Storage Account (queue) | `queue` | `privatelink.queue.core.windows.net` |
| PE-3 | Storage Account (table) | `table` | `privatelink.table.core.windows.net` |
| PE-4 | Cosmos DB | `Sql` | `privatelink.documents.azure.com` |
| PE-5 | Key Vault | `vault` | `privatelink.vaultcore.azure.net` |
| PE-6 | Document Intelligence | `account` | `privatelink.cognitiveservices.azure.com` |
| PE-7 | AI Language | `account` | `privatelink.cognitiveservices.azure.com` |

### Private DNS Zones (6 unique, linked to VNet)

- `privatelink.blob.core.windows.net`
- `privatelink.queue.core.windows.net`
- `privatelink.table.core.windows.net`
- `privatelink.documents.azure.com`
- `privatelink.vaultcore.azure.net`
- `privatelink.cognitiveservices.azure.com`

---

## Section 5: RBAC Role Assignments

### MI-1 (Functions Managed Identity) — Azure RBAC

| # | Resource | Role | Role Definition ID | Scope |
|---|---|---|---|---|
| RBAC-1 | Cosmos DB | Cosmos DB Built-in Data Contributor | `00000000-0000-0000-0000-000000000002` | Cosmos account |
| RBAC-2 | Storage Account | Storage Blob Data Owner | `b7e6dc6d-f1e8-4753-8033-0f276bb0955b` | Storage account |
| RBAC-3 | Storage Account | Storage Queue Data Contributor | `974c5e8b-45b9-4653-ba55-5f855dd0fb88` | Storage account |
| RBAC-4 | Storage Account | Storage Table Data Contributor | `0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3` | Storage account |
| RBAC-5 | Key Vault | Key Vault Secrets User | `4633458b-17de-408a-b874-0445c86b69e6` | Key Vault |
| RBAC-6 | Document Intelligence | Cognitive Services User | `a97b65f3-24c7-4388-baec-2e87135dc908` | Doc Intel account |
| RBAC-7 | AI Language | Cognitive Services User | `a97b65f3-24c7-4388-baec-2e87135dc908` | Language account |
| RBAC-8 | Azure OpenAI | Cognitive Services OpenAI User | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` | OpenAI account |
| RBAC-9 | App Insights | Monitoring Metrics Publisher | `3913510d-42f4-4e42-8a64-420c390055eb` | App Insights |
| RBAC-10 | Container Registry | AcrPull | `7f951dda-4ed3-4680-a7ca-43fe172d538d` | ACR |

### MI-1 — Microsoft Graph App Roles

| # | App Role | App Role ID | Assign to Resource | Purpose |
|---|---|---|---|---|
| GRAPH-1 | GroupMember.Read.All | `98830695-27a2-44f7-8c18-0c3ebc9698f6` | Microsoft Graph SP | ACL group verification |
| GRAPH-2 | User.Read.All | `df021288-bdef-4463-88db-98f22de89214` | Microsoft Graph SP | User profile resolution |

> **Note:** Microsoft Graph service principal object ID is tenant-specific. Get it with: `az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv`

### MI-2 (AKS Retrieval Identity) — Azure RBAC

| # | Resource | Role | Scope |
|---|---|---|---|
| RBAC-11 | Cosmos DB | Built-in Data Reader (`00000000-0000-0000-0000-000000000001`) | Cosmos account |
| RBAC-12 | Cosmos DB | Built-in Data Contributor (`00000000-0000-0000-0000-000000000002`) | `service-audit` container only |
| RBAC-13 | Azure OpenAI | Cognitive Services OpenAI User | OpenAI account |

### MI-2 — Microsoft Graph App Roles

| # | App Role | App Role ID |
|---|---|---|
| GRAPH-3 | GroupMember.Read.All | `98830695-27a2-44f7-8c18-0c3ebc9698f6` |
| GRAPH-4 | User.Read.All | `df021288-bdef-4463-88db-98f22de89214` |

---

## Section 6: Entra ID App Registrations

### App Registration 1: SharePoint Ingestion App

| Property | Value |
|---|---|
| Display Name | `RAG-SharePoint-Ingestion` |
| Sign-in Audience | AzureADMyOrg (single tenant) |
| Authentication | Certificate-based (no client secret) |

**API Permissions (Application type, admin consent required):**

| # | API | Permission | Permission ID |
|---|---|---|---|
| P-1 | Microsoft Graph | GroupMember.Read.All | `98830695-27a2-44f7-8c18-0c3ebc9698f6` |
| P-2 | Microsoft Graph | Sites.Read.All | `332a536c-c7ef-4017-ab91-336970924f0d` |
| P-3 | Microsoft Graph | Sites.Selected | `883ea226-0bf2-4a8f-9f9d-92c9162a727d` |
| P-4 | Microsoft Graph | User.Read.All | `df021288-bdef-4463-88db-98f22de89214` |
| P-5 | SharePoint (`00000003-0000-0ff1-ce00-000000000000`) | Sites.Read.All | `d13f72ca-a275-4b96-b789-48ebcc4da984` |

**Certificate:** Upload a self-signed or CA-signed certificate (2048-bit RSA, SHA256) to this app registration. The private key (PFX, exported without password) must be stored in Key Vault as secret `sharepoint-app-cert` with content type `application/x-pkcs12`.

### App Registration 2: Admin API (Easy Auth)

| Property | Value |
|---|---|
| Display Name | `RAG-Admin-API` |
| Sign-in Audience | AzureADMyOrg |
| Identifier URI | `api://<this-app-client-id>` |
| API Permissions | None |

---

## Section 7: Function App Easy Auth Configuration

| Setting | Value |
|---|---|
| Authentication | Enabled |
| Identity Provider | Microsoft Entra ID |
| Client ID | App Registration 2 client ID |
| Allowed Audiences | `api://<app-reg-2-client-id>` |
| Issuer | `https://login.microsoftonline.com/<tenant-id>/v2.0` |
| Unauthenticated requests | Return 401 |
| Excluded paths | `/api/webhook/*` |

---

## Section 8: SharePoint Configuration

| Item | Value | Responsible |
|---|---|---|
| SharePoint site URL | `https://<tenant>.sharepoint.com/sites/<site-name>` | SharePoint admin |
| Document library | Create or use existing | SharePoint admin |
| Library inheritance | Must inherit from site (default) | SharePoint admin |
| Security groups | Create 2 Entra SGs (e.g., Editors + Readers) | Entra admin |
| SGs added to site | Site Settings → Site Permissions → Add as Domain Group | SharePoint admin |

---

## Section 9: Key Vault Secret Upload

| Secret Name | Content Type | Value |
|---|---|---|
| `sharepoint-app-cert` | `application/x-pkcs12` | Base64-encoded PFX (no password) |

> Requires Key Vault Secrets Officer role on the uploader's identity. After upload, the managed identity (MI-1) reads it via Key Vault Secrets User (RBAC-5).

---

## Values to Return After Provisioning

Cloud Ops must return these values to the developer:

| # | Item | Example |
|---|---|---|
| 1 | Resource group name | `rg-rag-prod` |
| 2 | MI-1 client ID | `b7e87ad3-...` |
| 3 | MI-1 principal ID | `abc123-...` |
| 4 | MI-2 client ID (AKS) | `def456-...` |
| 5 | App Reg 1 client ID | `919be0bb-...` |
| 6 | App Reg 2 client ID | `f6a39f07-...` |
| 7 | Key Vault name | `rag-prod-kv-abc123` |
| 8 | Cosmos DB account name | `rag-prod-cosmos-abc123` |
| 9 | Cosmos DB endpoint | `https://rag-prod-cosmos-abc123.documents.azure.com:443/` |
| 10 | Function App name | `rag-prod-func-abc123` |
| 11 | Function App URL | `https://rag-prod-func-abc123.azurewebsites.net` |
| 12 | ACR login server | `ragprodacrabc123.azurecr.io` |
| 13 | AKS cluster name | `rag-prod-aks-abc123` |
| 14 | App Insights connection string | `InstrumentationKey=...` |
| 15 | OpenAI endpoint | `https://openai-account.openai.azure.com/` |
| 16 | Graph SP object ID | `<tenant-specific GUID>` |
| 17 | Tenant ID | `<tenant-id>` |
| 18 | SharePoint drive ID | `b!J4-sjv...` |
