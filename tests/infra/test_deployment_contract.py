from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "deploy.ps1"
PLAN = PROJECT_ROOT / ".azure" / "deployment-plan.md"
OPERATIONS_PARAMETERS = PROJECT_ROOT / "infra" / "operations.parameters.bicepparam"
FUNCTION_IGNORE = PROJECT_ROOT / "app" / ".funcignore"


def test_deployment_controller_has_no_implicit_or_destructive_target() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    required_environment = source[
        source.index("function Assert-RequiredEnvironment"):source.index("function Import-AzdEnvironment")
    ]

    assert "[Parameter(Mandatory)]" in source
    assert "[switch]$Execute" in source
    assert "az deployment group what-if" in source
    assert source.index("az deployment group what-if") < source.index("az deployment group create")
    assert "--mode Incremental" in source
    assert "az group create" not in source
    assert "azd down" not in source
    assert "az ad " not in source
    assert "az containerapp update" not in source
    assert "RETRIEVAL_IMAGE_NAME" not in source
    assert "rag-dev-webhook-secret" not in source
    assert "[string]$ResourceGroup," in source
    assert "[string]$ResourceGroup =" not in source
    assert "existing development environment" in source
    assert "repository@sha256:<64 lowercase hex>" in source
    assert "sha256:<64 lowercase hex>" in source
    assert "'Operations'" in source
    assert "'CatalogVerify'" in source
    assert "'OperationsCleanup'" in source
    assert "az containerapp job start" in source
    assert "az containerapp job execution show" in source
    assert "az containerapp job delete" in source
    assert "Invoke-OperationsInfrastructure" in source
    assert "$OperationsTemplatePath" in source
    assert "$OperationsParameterPath" in source
    assert "Get-SingleDeploymentResource" in source
    assert "'Final' { Invoke-InfrastructurePhase -Serving $true -Operations $false }" in source
    assert "Expected exactly one private operations job" in source
    assert "publish_retrieval_catalog.py" in source
    assert "'SHAREPOINT_SITE_URL'" in required_environment


def test_operations_job_parameters_use_only_reviewed_existing_inputs() -> None:
    source = OPERATIONS_PARAMETERS.read_text(encoding="utf-8")

    assert "using './modules/aca-operations-job.bicep'" in source
    assert "readEnvironmentVariable('RETRIEVAL_IMAGE_REFERENCE')" in source
    assert "readEnvironmentVariable('RETRIEVAL_CATALOG_DIGEST')" in source
    assert "readEnvironmentVariable('MANAGED_ENVIRONMENT_ID')" in source
    assert "readEnvironmentVariable('OPERATIONS_MANAGED_IDENTITY_ID')" in source
    assert "readEnvironmentVariable('COSMOS_ENDPOINT')" in source
    assert "Temporary: 'true'" in source


def test_function_package_excludes_local_python_environments_and_settings() -> None:
    patterns = FUNCTION_IGNORE.read_text(encoding="utf-8").splitlines()

    assert ".venv/" in patterns
    assert ".venv-*/" in patterns
    assert "local.settings.json" in patterns


def test_deployment_plan_rejects_stale_target_authority() -> None:
    plan = PLAN.read_text(encoding="utf-8")

    assert "Plan ID: `aca-greenfield-retrieval-v1`" in plan
    assert "> **Status:** Validated" in plan
    assert "Stage 5 requires separate explicit approval" in plan
    assert "existing development environment" not in plan


def test_authority_mode_is_local_and_machine_readable() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required for deployment controller tests")

    result = subprocess.run(
        [powershell, "-NoProfile", "-File", str(SCRIPT), "-Phase", "Authority"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["planId"] == "aca-greenfield-retrieval-v1"
    assert len(payload["planHash"]) == 64
    assert len(payload["sourceTreeHash"]) == 64
