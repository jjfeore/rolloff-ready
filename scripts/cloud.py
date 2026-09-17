"""Azure deployment helpers. No customer data or credentials are printed.

Azure CLI must already be authenticated. Run from the repository root.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

PROJECT = "rolloff-ready"
MANAGER = "rolloff-ready-bicep"
ROOT = Path(__file__).resolve().parents[1]


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def az(*args: str):
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Install Azure CLI and sign in before running this helper.")
    result = subprocess.run(
        [executable, *args, "--only-show-errors", "--output", "json"],
        capture_output=True, text=True, check=False, cwd=ROOT,
    )
    if result.returncode:
        # Azure error bodies can echo settings. Do not echo raw stdout/stderr.
        raise RuntimeError(f"Azure CLI {' '.join(args[:2])} failed (exit {result.returncode}). Check the Azure activity/deployment log privately.")
    if args[:2] == ("bicep", "install"):
        return None  # This CLI extension writes plain progress text even with --output json.
    return json.loads(result.stdout) if result.stdout.strip() else None


def verified_group() -> dict:
    name = required("AZURE_RESOURCE_GROUP")
    repository = required("GITHUB_REPOSITORY")
    subscription = required("AZURE_SUBSCRIPTION_ID")
    if not re.fullmatch(r"rg-rolloff-ready(?:-[a-z0-9-]+)?", name):
        raise RuntimeError("Group name must be rg-rolloff-ready or rg-rolloff-ready-<suffix>.")
    group = az("group", "show", "--name", name, "--subscription", subscription)
    expected_id = f"/subscriptions/{subscription}/resourceGroups/{name}"
    if group["id"].lower() != expected_id.lower():
        raise RuntimeError("Resource group subscription/name mismatch.")
    tags = group.get("tags") or {}
    expected = {"project": PROJECT, "managedBy": MANAGER, "repository": repository}
    if any(tags.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Ownership tags do not match this project and repository. Refusing to modify group.")
    return group


def append_output(name: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise RuntimeError("Unexpected newline in workflow output.")
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def provision(enable_email: bool) -> None:
    group = verified_group()
    app_secret = required("APP_SECRET")
    if len(app_secret) < 32:
        raise RuntimeError("APP_SECRET must contain at least 32 characters.")
    values = {
        "repository": required("GITHUB_REPOSITORY"),
        "location": os.environ.get("AZURE_LOCATION", "westus2"),
        "enableEmail": enable_email,
        "hereMapsApiKey": required("HERE_MAPS_API_KEY"),
        "appSecret": app_secret,
    }
    parameters = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#", "contentVersion": "1.0.0.0", "parameters": {key: {"value": value} for key, value in values.items()}}
    # File avoids secrets in CLI arguments; secure parameters hide values in ARM history.
    # NamedTemporaryFile opens exclusively with restricted permissions and also
    # works with Windows ACLs when the CLI must reopen the file.
    with tempfile.NamedTemporaryFile(mode="w", prefix="rolloff-provision-", suffix=".json", encoding="utf-8", delete=False) as temp:
        json.dump(parameters, temp)
        path = Path(temp.name)
    try:
        result = az("deployment", "group", "create", "--resource-group", group["name"], "--subscription", required("AZURE_SUBSCRIPTION_ID"), "--name", PROJECT, "--mode", "Incremental", "--template-file", str(ROOT / "infra" / "main.bicep"), "--parameters", f"@{path}")
    finally:
        path.unlink(missing_ok=True)
    outputs = result["properties"]["outputs"]
    for source, destination in (("staticWebAppName", "site"), ("hostname", "hostname"), ("emailSender", "email_sender")):
        value = outputs[source]["value"]
        append_output(destination, value)
        print(f"{destination}: {value}")


def deployment_token() -> None:
    group = verified_group()
    sites = az("staticwebapp", "list", "--resource-group", group["name"], "--subscription", required("AZURE_SUBSCRIPTION_ID"))
    expected_tags = {"project": PROJECT, "managedBy": MANAGER, "repository": required("GITHUB_REPOSITORY")}
    sites = [site for site in sites if all((site.get("tags") or {}).get(key) == value for key, value in expected_tags.items())]
    if len(sites) != 1:
        raise RuntimeError("Expected exactly one project Static Web App. Run Provision Azure first.")
    site = sites[0]
    result = az("staticwebapp", "secrets", "list", "--name", site["name"], "--resource-group", group["name"], "--subscription", required("AZURE_SUBSCRIPTION_ID"))
    token = result["properties"]["apiKey"]
    if not os.environ.get("GITHUB_OUTPUT") or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Deployment token output is available only within GitHub Actions.")
    print(f"::add-mask::{token}")
    append_output("token", token)
    append_output("hostname", site["defaultHostname"])


def teardown(confirmation: str) -> None:
    if confirmation != "DELETE rolloff-ready":
        raise RuntimeError("Type exactly DELETE rolloff-ready to authorize teardown.")
    group = verified_group()
    resources = az("resource", "list", "--resource-group", group["name"], "--subscription", required("AZURE_SUBSCRIPTION_ID"))
    allowed_types = {"microsoft.web/staticsites", "microsoft.communication/emailservices", "microsoft.communication/emailservices/domains", "microsoft.communication/communicationservices"}
    for resource in resources:
        tags = resource.get("tags") or {}
        if resource["type"].lower() not in allowed_types or any(tags.get(key) != value for key, value in {"project": PROJECT, "managedBy": MANAGER, "repository": required("GITHUB_REPOSITORY")}.items()):
            raise RuntimeError("Group contains an unrecognized or untagged resource. Refusing teardown; inspect the group in Azure.")
    az("group", "delete", "--name", group["name"], "--subscription", required("AZURE_SUBSCRIPTION_ID"), "--yes")
    print("Deleted the verified Rolloff Ready resource group. Repository and Entra application remain.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    provision_parser = commands.add_parser("provision")
    provision_parser.add_argument("--enable-email", choices=("true", "false"), default="true")
    commands.add_parser("deployment-token")
    commands.add_parser("verify-group")
    teardown_parser = commands.add_parser("teardown")
    teardown_parser.add_argument("--confirmation", required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "provision":
            provision(arguments.enable_email == "true")
        elif arguments.command == "deployment-token":
            deployment_token()
        elif arguments.command == "teardown":
            teardown(arguments.confirmation)
        else:
            verified_group()
            print("Resource group ownership verified.")
    except (RuntimeError, KeyError, ValueError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
