"""Pre-deployment and post-deployment validation script.

Usage:
  python scripts/validate_deployment.py --pre    # Before deployment: check config + connectivity
  python scripts/validate_deployment.py --post   # After deployment: verify function app health
"""

from __future__ import annotations

import argparse
import os
import sys

REQUIRED_ENV_VARS = [
    "INGESTION_SOURCE_ID",
    "SHAREPOINT_ASSIGNED_DRIVE_ID",
    "SHAREPOINT_TENANT_ID",
    "SHAREPOINT_APP_CLIENT_ID",
    "KEY_VAULT_URI",
    "COSMOS_ENDPOINT",
    "COSMOS_DATABASE_NAME",
    "OPENAI_ENDPOINT",
    "DOCUMENT_INTELLIGENCE_ENDPOINT",
    "AZURE_LANGUAGE_ENDPOINT",
]

OPTIONAL_ENV_VARS = [
    "AZURE_CLIENT_ID",
    "SHAREPOINT_CERTIFICATE_SECRET_NAME",
    "COSMOS_INGESTION_RUNS_CONTAINER_NAME",
    "COSMOS_SOURCE_DOCUMENTS_CONTAINER_NAME",
    "COSMOS_SEARCH_CHUNKS_CONTAINER_NAME",
    "ALLOWED_FILE_EXTENSIONS",
    "EXTRACTION_ENABLED",
    "ENRICHMENT_ENABLED",
    "WAVE_SIZE",
]


def validate_pre_deployment() -> bool:
    """Validate environment variables and basic connectivity."""
    print("=== Pre-Deployment Validation ===\n")
    passed = True

    # Check required env vars
    print("Checking required environment variables...")
    for var in REQUIRED_ENV_VARS:
        value = os.getenv(var, "")
        if value:
            print(f"  ✓ {var} = {value[:30]}{'...' if len(value) > 30 else ''}")
        else:
            print(f"  ✗ {var} is NOT SET")
            passed = False

    print("\nChecking optional environment variables...")
    for var in OPTIONAL_ENV_VARS:
        value = os.getenv(var, "")
        status = f"= {value}" if value else "(using default)"
        print(f"  · {var} {status}")

    # Check Python dependencies
    print("\nChecking Python dependencies...")
    required_packages = [
        "azure.functions",
        "azure.durable_functions",
        "azure.cosmos",
        "azure.identity",
        "azure.ai.documentintelligence",
        "azure.ai.textanalytics",
        "openai",
        "httpx",
        "tiktoken",
    ]
    for pkg in required_packages:
        try:
            __import__(pkg)
            print(f"  ✓ {pkg}")
        except ImportError:
            print(f"  ✗ {pkg} NOT INSTALLED")
            passed = False

    # Check app compiles
    print("\nChecking app compilation...")
    import subprocess
    app_files = [
        "app/function_app.py",
        "app/config.py",
        "app/ingestion/services.py",
        "app/ingestion/extraction.py",
        "app/ingestion/chunking.py",
        "app/ingestion/enrichment.py",
        "app/ingestion/embedding.py",
    ]
    for f in app_files:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", f],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  ✓ {f}")
        else:
            print(f"  ✗ {f}: {result.stderr.strip()}")
            passed = False

    print(f"\n{'✓ PASSED' if passed else '✗ FAILED'}")
    return passed


def validate_post_deployment() -> bool:
    """Validate deployed function app is healthy."""
    print("=== Post-Deployment Validation ===\n")
    passed = True

    try:
        import subprocess
        import json

        # Check function app exists and is running
        func_name = os.getenv("FUNCTION_APP_NAME", "")
        rg = os.getenv("AZURE_RESOURCE_GROUP", "")
        if not func_name or not rg:
            print("  ✗ FUNCTION_APP_NAME or AZURE_RESOURCE_GROUP not set")
            print("    Set these or use: az functionapp list --resource-group <rg>")
            return False

        result = subprocess.run(
            ["az", "functionapp", "show", "--name", func_name, "--resource-group", rg, "--query", "{state:state,hostName:defaultHostName}", "-o", "json"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  ✗ Cannot query function app: {result.stderr.strip()}")
            return False

        info = json.loads(result.stdout)
        print(f"  Function App: {func_name}")
        print(f"  State: {info.get('state', 'unknown')}")
        print(f"  Host: {info.get('hostName', 'unknown')}")

        if info.get("state") != "Running":
            print("  ✗ Function app is not running")
            passed = False

        # Check functions are discovered
        result = subprocess.run(
            ["az", "functionapp", "function", "list", "--name", func_name, "--resource-group", rg, "--query", "[].name", "-o", "json"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            functions = json.loads(result.stdout)
            expected = ["start_full_sync", "full_sync_orchestrator", "activate_run_activity", "discover_all_activity", "process_document_activity", "finalize_run_activity"]
            print(f"\n  Discovered functions: {len(functions)}")
            for fn in expected:
                found = any(fn in f for f in functions)
                print(f"    {'✓' if found else '✗'} {fn}")
                if not found:
                    passed = False

        # Check Cosmos containers exist
        print("\n  Checking Cosmos containers...")
        cosmos_name = os.getenv("COSMOS_ACCOUNT_NAME", "")
        if cosmos_name:
            result = subprocess.run(
                ["az", "cosmosdb", "sql", "container", "list", "--account-name", cosmos_name, "--resource-group", rg, "--database-name", os.getenv("COSMOS_DATABASE_NAME", "rag-db"), "--query", "[].name", "-o", "json"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                containers = json.loads(result.stdout)
                for c in ["ingestion-runs", "source-documents", "search-chunks"]:
                    found = c in containers
                    print(f"    {'✓' if found else '✗'} {c}")
                    if not found:
                        passed = False

    except Exception as error:
        print(f"  ✗ Validation error: {error}")
        passed = False

    print(f"\n{'✓ PASSED' if passed else '✗ FAILED'}")
    return passed


def main():
    parser = argparse.ArgumentParser(description="Deployment validation")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pre", action="store_true", help="Pre-deployment checks")
    group.add_argument("--post", action="store_true", help="Post-deployment checks")
    args = parser.parse_args()

    if args.pre:
        success = validate_pre_deployment()
    else:
        success = validate_post_deployment()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
