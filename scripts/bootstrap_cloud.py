"""One-time, explicitly invoked Azure/GitHub setup. Does not provision billable services.

Requires authenticated Azure CLI and GitHub CLI. See .planning/USER-SETUP.md.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile

from cloud import ROOT, az, verified_group

CONTRIBUTOR_ROLE = "b24988ac-6180-42a0-ab88-20f7382dd24c"


def gh(*args: str, stdin: str | None = None, allow_missing: bool = False):
    executable = shutil.which("gh")
    if not executable:
        raise RuntimeError("Install GitHub CLI and run gh auth login first.")
    result = subprocess.run([executable, *args], input=stdin, capture_output=True, text=True)
    if result.returncode:
        if allow_missing and "HTTP 404" in result.stderr:
            return None
        raise RuntimeError(f"GitHub CLI {' '.join(args[:2])} failed. Check repository access and environment support.")
    return json.loads(result.stdout) if result.stdout.strip().startswith(("{", "[")) else result.stdout.strip()


def read_settings(path: Path) -> dict[str, str]:
    allowed = {"AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "HERE_MAPS_API_KEY", "APP_SECRET"}
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in allowed:
            result[key] = value
    return result


def ensure_environment(repository: str) -> None:
    route = f"repos/{repository}/environments/production"
    environment = gh("api", route, allow_missing=True)
    if environment is None:
        gh("api", route, "--method", "PUT", "--input", "-", stdin=json.dumps({"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}))
        gh("api", f"{route}/deployment-branch-policies", "--method", "POST", "--input", "-", stdin=json.dumps({"name": "main", "type": "branch"}))
    else:
        policy = environment.get("deployment_branch_policy") or {}
        branches = gh("api", f"{route}/deployment-branch-policies")["branch_policies"]
        if not policy.get("custom_branch_policies") or len(branches) != 1 or branches[0].get("name") != "main" or branches[0].get("type") != "branch":
            raise RuntimeError("Existing production environment must allow only the main branch. Configure Settings > Environments > production; existing protection rules are preserved.")


def audit_project_roles(principal_id: str, subscription_id: str, scope: str) -> list[dict]:
    """Reject unexpected grants before adding GitHub federation to an identity.

    --all includes subscription descendants. A separate subscription-scope
    query with --include-inherited explicitly adds ancestors; --include-groups
    covers transitive group assignments. Failure to
    enumerate any of these fails closed through az(), rather than assuming an
    empty set. Azure CLI's visibility is limited to this subscription/ancestors.
    """
    query = (
        "role", "assignment", "list", "--assignee", principal_id,
        "--subscription", subscription_id,
        "--include-groups", "--fill-principal-name", "false",
        "--fill-role-definition-name", "false",
    )
    assignments = az(*query, "--all")
    assignments += az(*query, "--scope", f"/subscriptions/{subscription_id}", "--include-inherited")
    for item in assignments:
        if (
            item.get("principalId", "").lower() != principal_id.lower()
            or item.get("roleDefinitionId", "").rsplit("/", 1)[-1].lower() != CONTRIBUTOR_ROLE
            or item.get("scope", "").lower() != scope.lower()
            or item.get("condition")
        ):
            raise RuntimeError(
                "Deployment identity has an unexpected Azure role assignment: "
                f"role {item.get('roleDefinitionId', 'unknown')} at {item.get('scope', 'unknown')}. "
                "Only direct Contributor on this project's resource group is allowed. "
                "No roles were removed and no new federation was added. Use a new dedicated "
                "application (omit the client reuse option; select --new-application if a "
                "same-name app exists), or have an administrator review the existing grants."
            )
    return assignments


def bootstrap(args) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise RuntimeError("Repository must be owner/repository.")
    if not re.fullmatch(r"rg-rolloff-ready(?:-[a-z0-9-]+)?", args.resource_group):
        raise RuntimeError("Use a dedicated rg-rolloff-ready or rg-rolloff-ready-<suffix> group.")
    settings = read_settings(Path(args.env_file))
    for name in ("AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID", "HERE_MAPS_API_KEY"):
        if not settings.get(name):
            raise RuntimeError(f"Add {name} to the private environment file.")
    for name in ("AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID"):
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", settings[name]):
            raise RuntimeError(f"{name} must be an Azure identifier.")
    os.environ.update({"AZURE_SUBSCRIPTION_ID": settings["AZURE_SUBSCRIPTION_ID"], "AZURE_RESOURCE_GROUP": args.resource_group, "GITHUB_REPOSITORY": args.repository})
    if args.login:
        # User-invoked browser sign-in. Azure CLI handles credentials directly.
        executable = shutil.which("az")
        if not executable or subprocess.run([executable, "login", "--tenant", settings["AZURE_TENANT_ID"], "--output", "none"]).returncode:
            raise RuntimeError("Azure interactive sign-in did not complete.")
    account = az("account", "show")
    if account["tenantId"].lower() != settings["AZURE_TENANT_ID"].lower():
        raise RuntimeError("Azure CLI is signed in to another tenant. Sign in to the configured tenant first.")
    az("account", "set", "--subscription", settings["AZURE_SUBSCRIPTION_ID"])
    ensure_environment(args.repository)
    print("GitHub production environment permits only main.")
    if az("group", "exists", "--name", args.resource_group):
        verified_group()
    else:
        az("bicep", "install", "--version", "v0.47.16")
        az("deployment", "sub", "create", "--name", "rolloff-ready-bootstrap", "--location", args.location, "--template-file", str(ROOT / "infra" / "bootstrap.bicep"), "--parameters", f"resourceGroupName={args.resource_group}", f"location={args.location}", f"repository={args.repository}")
    print("Dedicated resource group created or verified.")
    for namespace in ("Microsoft.Web", "Microsoft.Communication"):
        az("provider", "register", "--namespace", namespace, "--wait")
    client_id = args.client_id
    if args.reuse_configured_client:
        client_id = settings.get("AZURE_CLIENT_ID")
        if not client_id:
            raise RuntimeError("The private environment file has no AZURE_CLIENT_ID to reuse.")
    if client_id:
        app = az("ad", "app", "show", "--id", client_id)
    else:
        name = f"rolloff-ready-github-{args.repository.replace('/', '-')}"
        if args.new_application:
            name = f"{name}-{secrets.token_hex(4)}"
        existing = [] if args.new_application else az("ad", "app", "list", "--display-name", name)
        if len(existing) > 1:
            raise RuntimeError("Multiple matching Entra applications. Select the intended dedicated application with --client-id.")
        app = existing[0] if existing else az("ad", "app", "create", "--display-name", name)
    principal = az("ad", "sp", "list", "--filter", f"appId eq '{app['appId']}'")
    principal = principal[0] if principal else az("ad", "sp", "create", "--id", app["appId"])
    scope = f"/subscriptions/{settings['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{args.resource_group}"
    assignments = audit_project_roles(principal["id"], settings["AZURE_SUBSCRIPTION_ID"], scope)
    credential = {"name": "rolloff-ready-production", "issuer": "https://token.actions.githubusercontent.com", "subject": f"repo:{args.repository}:environment:production", "audiences": ["api://AzureADTokenExchange"]}
    credentials = az("ad", "app", "federated-credential", "list", "--id", app["id"])
    existing = [item for item in credentials if item["name"] == credential["name"]]
    if existing:
        if any(existing[0].get(key) != value for key, value in credential.items()):
            raise RuntimeError("Existing OIDC federation differs. Refusing to overwrite another trust relationship.")
    else:
        with tempfile.NamedTemporaryFile(mode="w", prefix="rolloff-oidc-", suffix=".json", encoding="utf-8", delete=False) as temp:
            json.dump(credential, temp)
            path = Path(temp.name)
        try:
            az("ad", "app", "federated-credential", "create", "--id", app["id"], "--parameters", f"@{path}")
        finally:
            path.unlink(missing_ok=True)
    if not assignments:
        az("role", "assignment", "create", "--assignee-object-id", principal["id"], "--assignee-principal-type", "ServicePrincipal", "--role", CONTRIBUTOR_ROLE, "--scope", scope)
    values = {"AZURE_SUBSCRIPTION_ID": settings["AZURE_SUBSCRIPTION_ID"], "AZURE_TENANT_ID": settings["AZURE_TENANT_ID"], "AZURE_CLIENT_ID": app["appId"], "HERE_MAPS_API_KEY": settings["HERE_MAPS_API_KEY"]}
    secret_names = {item["name"] for item in gh("secret", "list", "--repo", args.repository, "--env", "production", "--json", "name")}
    if settings.get("APP_SECRET"):
        if len(settings["APP_SECRET"]) < 32:
            raise RuntimeError("APP_SECRET needs at least 32 characters.")
        values["APP_SECRET"] = settings["APP_SECRET"]
    elif "APP_SECRET" not in secret_names:
        values["APP_SECRET"] = secrets.token_urlsafe(48)
    for name, value in values.items():
        gh("secret", "set", name, "--repo", args.repository, "--env", "production", stdin=value)
    for name, value in {"AZURE_RESOURCE_GROUP": args.resource_group, "AZURE_LOCATION": args.location, "AZURE_DEPLOY_ENABLED": "true"}.items():
        gh("variable", "set", name, "--repo", args.repository, "--body", value)
    print("OIDC and GitHub configuration complete. No Azure application services have been provisioned.")
    print("Next: run Provision Azure on main, then Deploy Azure. Email incurs usage charges when enabled.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--resource-group", default="rg-rolloff-ready")
    parser.add_argument("--location", default="westus2")
    client = parser.add_mutually_exclusive_group()
    client.add_argument("--client-id", help="Explicitly reuse an existing dedicated Entra application; otherwise create one.")
    client.add_argument("--reuse-configured-client", action="store_true", help="Explicitly reuse AZURE_CLIENT_ID from the private environment file.")
    client.add_argument("--new-application", action="store_true", help="Create a fresh dedicated app with a unique name instead of finding/reusing one.")
    parser.add_argument("--login", action="store_true", help="Start Azure's interactive browser sign-in for the configured tenant.")
    arguments = parser.parse_args()
    try:
        bootstrap(arguments)
    except (RuntimeError, KeyError, ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
