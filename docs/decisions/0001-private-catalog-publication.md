# Private Retrieval Catalog Publication

## Status

Accepted. This record documents the existing implementation.

## Context

Retrieval scoring profiles, freshness settings, synonym maps, and shared retrieval tuning are stored as immutable items in the private Cosmos DB `retrieval-config` container. Cosmos public access and local key authentication are disabled. The deployment must publish a reviewed catalog without giving an external deployment runner a Cosmos key or requiring direct access to the private endpoint.

## Decision

The guarded deployment creates a temporary manual Azure Container Apps job in the existing private ACA environment. The job:

1. Pulls the reviewed immutable retrieval image from ACR using the Operations UAMI.
2. Validates the catalog content against the expected SHA-256 digest.
3. Publishes the immutable item to the deployment-instance partition in `retrieval-config`.
4. Creates or verifies the ETag-protected publication pointer.
5. Is removed by `OperationsCleanup` after publication, serving deployment, and E2E verification succeed.

The Retrieval Container App does not use the job at query time. It loads the catalog item named by `RETRIEVAL_CATALOG_DIGEST` at startup and fails readiness when the item, digest, deployment instance, schema, profile references, or synonym-map references are invalid.

## Consequences

- Hosted deployment runners do not need direct network access or credentials for private Cosmos DB.
- The Operations UAMI requires ACR pull access and Cosmos data-contributor access scoped to `retrieval-config`.
- Catalog publication adds deployment phases for job creation, execution, verification, and cleanup.
- Removing the temporary job does not remove the published catalog or affect running retrieval replicas.

## Implementation

- Job resource: [`infra/modules/aca-operations-job.bicep`](../../infra/modules/aca-operations-job.bicep)
- Publisher: [`app/retrieval/operations.py`](../../app/retrieval/operations.py)
- Deployment controller: [`scripts/deploy.ps1`](../../scripts/deploy.ps1)
- Catalog contract: [`app/retrieval/catalog.py`](../../app/retrieval/catalog.py)
- Runtime loading: [`app/retrieval/main.py`](../../app/retrieval/main.py)
