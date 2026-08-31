# Production Readiness

This document separates verified release evidence from production-scale work that remains unproven. It does not convert development estimates into production guarantees.

## Current Verified Baseline

The current release deployment validation was completed on 2026-08-31. Detailed environment-specific evidence is retained outside source control; the non-sensitive results are summarized below.

Verified against the reviewed non-production deployment:

- ACA revision and immutable image digest remained stable through validation.
- Immutable retrieval catalog digest was used by all audited scenarios.
- The primary scoring profile returned a grounded answer with citations through the Function gateway.
- A no-change full sync completed successfully with no failed documents; sampled ready manifests and retrievable chunks satisfied lifecycle invariants.
- Synonym enabled/disabled requests selected the expected map state with no degraded retrieval.
- The complete local suite passed. Opt-in live-Cosmos integration tests remained skipped.
- Complete four-profile, denied-principal, freshness-fixture, delta update/delete, restart, and recovery evidence was not repeated against the final release artifact and remains required before those behaviors can be claimed for this release.
- The temporary catalog publisher job was removed after explicit approval.

These results prove functional behavior for the tested corpus and target. They do not prove production capacity, SLOs, disaster recovery, or multi-region behavior.

## Release Artifact Contract

A release is one reviewed tuple:

- Source-tree hash and deployment-plan hash.
- Immutable retrieval image `repository@sha256:<digest>`.
- Immutable retrieval catalog `sha256:<digest>`.
- Function package built from the same reviewed source.
- Exact target subscription, tenant, resource group, region, azd environment, and deployment instance.

Use `scripts/deploy.ps1`; do not run direct `azd provision`, `az containerapp update`, or `func azure functionapp publish` as production release steps.

Rollback is a new reviewed deployment of a compatible prior tuple. Changing the catalog publication pointer alone does not change runtime selection.

## Required Pre-Release Gates

1. Local unit, contract, and Bicep checks pass.
2. `Authority` returns reviewed plan/source hashes.
3. `Foundation`, `Operations`, and `Final` what-if output is reviewed.
4. External Entra applications, app roles, Graph permissions, Key Vault certificate, and SharePoint site grant are verified without exposing secrets.
5. ACR returns an immutable image digest.
6. Catalog validation returns the expected immutable digest.
7. Function and ACA authentication configurations match the exact audience/application/principal contracts.
8. Database schemas and partition keys are compatible with the deployed code.
9. Monitoring is available for requests, dependencies, exceptions, and ACA logs.

## Required Post-Deployment Gates

- Verify Function and ACA health, revision, traffic, image digest, catalog digest, and `ACL_ENABLED`.
- Run authenticated and unauthenticated gateway tests.
- Run authorized and denied ACL retrieval tests.
- Validate full sync, delta update/delete, ACL resync/restoration, and lifecycle reconciliation as applicable to the release.
- Run standard and agentic retrieval across required modes.
- Validate all deployed scoring profiles, freshness, and synonyms.
- Verify request/audit correlation and no unexpected dependency failures.
- Delete all test fixtures and prove manifest/chunk/query cleanup.
- Remove the temporary catalog job after explicit approval and run a final smoke test.

## Security Gaps and Boundaries

| Area | Current state | Release impact |
| --- | --- | --- |
| Function client allowlist | Required in Bicep | Verify exact approved caller before release |
| Per-user admin authorization | Not enforced on destructive Function endpoints | Production release requires risk acceptance or implementation of app-role checks |
| Lifecycle webhook | Excluded from EasyAuth and does not validate `clientState` | Security gap requiring risk acceptance or remediation |
| Retrieval gateway | ACA and application code restrict calls to Function UAMI | Verify app-role assignment externally because Bicep does not mutate Entra |
| ACL model | Entra security groups only | Direct user shares are unsupported |
| Rate limiting | In-memory per ACA replica | Not a hard distributed abuse control |
| Secrets | Certificate in external Key Vault; webhook client state in secure app setting | Verify external vault/network policy and secret rotation process |
| Query audit data | Stores user/tenant IDs, up to 2,000 question characters, and a 500-character answer preview for the 90-day container TTL; inspect can return these records | Apply privacy classification, least-privilege endpoint access, retention approval, and safe diagnostic handling |

## Capacity and Reliability Evidence Still Required

No current repository artifact proves the following for a production workload:

- Maximum sustainable file count, pages, chunks, or concurrent source changes.
- Cosmos RU/s, throttling envelope, partition hot spots, or query RU distribution.
- Azure OpenAI, Document Intelligence, or Language quota required for peak ingestion/query traffic.
- p50/p95/p99 latency, throughput, saturation, or error-rate SLOs.
- Scale-out behavior of the per-replica rate limiter.
- Zone-failure, regional-failure, restore, backup, or disaster-recovery objectives.
- Multi-library operation from one Function deployment.

Treat any prior 10K-file duration, RU, or concurrency number as a planning hypothesis until a representative load test records inputs, duration, throttles, costs, and recovery behavior.

## Production Capacity Plan

Before production approval:

1. Define corpus size, document-size/page distributions, change rate, query concurrency, latency SLOs, RTO/RPO, and cost ceiling.
2. Select serverless or provisioned Cosmos mode from measured RU demand; do not assume a fixed RU value is sufficient.
3. Validate Azure OpenAI and AI Services quotas in the target region.
4. Run staged load tests with representative documents and ACL distributions.
5. Measure Function/ACA scale, dependency throttling, Cosmos RU, latency percentiles, and failure recovery.
6. Run lifecycle reconciliation and restart recovery under injected partial failures.
7. Record halt criteria and the compatible immutable rollback tuple.

## Evaluation Gate

Protected evaluation requires approved ground truth, principal cases, dataset manifest, and SME approval. Run `evaluation.generate_rankings` and `evaluation.retrieval_metrics` with the required experiment manifest. Declare Precision@K, Recall@K, MRR, and per-query regression thresholds before examining candidate results.

The evaluator verifies ranking/ground-truth hashes bound by the manifest. Source-tree, image, dependency, catalog, approval, and submitted-context hashes remain release-gate responsibilities outside the evaluator.

## Readiness Verdict

- **Current-release deployment smoke:** passed.
- **Complete same-release functional E2E:** pending the unrepeated gates identified above.
- **General production readiness:** conditional.
- **Blocking evidence for production scale:** workload requirements, capacity/load evidence, recovery objectives/tests, and acceptance of or fixes for the listed security gaps.
