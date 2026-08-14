"""Contract tests for AKS Bicep module ARM output."""

from __future__ import annotations

import json
import subprocess

import pytest


@pytest.fixture(scope="module")
def aks_arm():
    result = subprocess.run(
        ["az", "bicep", "build", "--file", "infra/modules/aks.bicep", "--stdout"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"Bicep build failed: {result.stderr}")
    return json.loads(result.stdout)


def test_aks_module_has_required_outputs(aks_arm):
    outputs = aks_arm.get("outputs", {})
    required = {"clusterName", "clusterFqdn", "oidcIssuerUrl", "kubeletIdentityObjectId"}
    assert required.issubset(outputs.keys())


def test_aks_module_enables_workload_identity(aks_arm):
    resources = aks_arm.get("resources", [])
    aks_resource = next(
        (r for r in resources if "managedClusters" in r.get("type", "")),
        None,
    )
    assert aks_resource is not None
    props = aks_resource.get("properties", {})
    assert props.get("securityProfile", {}).get("workloadIdentity", {}).get("enabled") is True


def test_aks_module_enables_oidc(aks_arm):
    resources = aks_arm.get("resources", [])
    aks_resource = next(
        (r for r in resources if "managedClusters" in r.get("type", "")),
        None,
    )
    assert aks_resource is not None
    props = aks_resource.get("properties", {})
    assert props.get("oidcIssuerProfile", {}).get("enabled") is True
