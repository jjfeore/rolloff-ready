# Azure deployment

The app uses Azure Static Web Apps **Free** with its managed Python 3.11 HTTP API. There is no separately billed Function App, storage account, database, or Application Insights resource. `main.bicep` optionally creates Azure Communication Services Email and an Azure-managed domain; email is usage-priced. HERE usage is billed or quota-limited according to the account's plan. This architecture is inexpensive for a small demo, but is not entirely free.

## One-time setup

Install Azure CLI, GitHub CLI, and Python 3.11+. Authenticate GitHub CLI with repository and workflow access. Use a dedicated Azure resource group. The Azure user running bootstrap needs permission to create that group and assign roles (for example, subscription Owner) and permission to create/manage the chosen Entra application. GitHub's workflow identity only receives Contributor on that group, not on the subscription.

Create a private, ignored `.planning/.env.cloud` containing `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, and `HERE_MAPS_API_KEY`. An optional `AZURE_CLIENT_ID` can name an existing dedicated app registration; otherwise bootstrap creates one. Optional `APP_SECRET` must contain at least 32 unpredictable characters; bootstrap securely generates one if it is absent. Existing GitHub `APP_SECRET` is preserved on subsequent runs. Never put an Azure client secret in GitHub: authentication uses OIDC.

From the repository root:

```powershell
$env:AZURE_CONFIG_DIR = Join-Path $PWD '.planning/azure-user'
$env:AZURE_LOGGING_ENABLE_LOG_FILE = 'false'
$env:AZURE_CORE_COLLECT_TELEMETRY = 'no'
python scripts/bootstrap_cloud.py --repository OWNER/REPOSITORY --env-file .planning/.env.cloud --login
```

Add `--reuse-configured-client` only when `AZURE_CLIENT_ID` identifies the dedicated application you intend to grant this repository access to. The bootstrap creates/verifies the resource group and GitHub `production` environment, restricts that environment to `main`, creates its OIDC federation, assigns the resource-group role, and writes GitHub secrets over stdin. It does not create the app or email service. It refuses to take over a group with different ownership tags or an unrelated federation. Existing GitHub environment protection rules are preserved; if its allowed branch policy differs, set it to only `main` and rerun.

Bootstrap reads the repository's OIDC settings to resolve the exact production subject. New repositories use `repo:OWNER@OWNER-ID/REPOSITORY@REPOSITORY-ID:environment:production`; older repositories may use `repo:OWNER/REPOSITORY:environment:production`. It can upgrade this project's known legacy production credential to its current immutable subject, preserving the credential name, issuer, audience, and environment restriction. Other mismatches and custom subject templates stop setup. If Azure login fails with `AADSTS700213`, pull the current code and rerun bootstrap, then rerun the failed workflow. The Node runtime deprecation notice is unrelated to this subject mismatch. See [GitHub's OIDC change](https://github.blog/changelog/2026-04-23-immutable-subject-claims-for-github-actions-oidc-tokens/).

Before creating federation, bootstrap audits the identity's assignments throughout the target subscription and explicitly queries inherited parent scopes, including transitive group grants. It allows only direct Contributor on the exact project group, or no existing assignment. Broader roles, roles on another group/resource, group-derived grants, and failed audits stop setup without removing any roles. Use `--new-application` instead of either reuse option to create a fresh dedicated identity when the old one has other responsibilities. This check is limited to the target subscription and its ancestors; it does not prove absence of grants in other subscriptions, delegated Lighthouse access, or Entra directory/application permissions. Have an administrator confirm a reused identity has no such permissions, or use a fresh app.

The GitHub repository must support environments (public repositories support them; private repositories depend on the GitHub plan). Bootstrap enables deployment only after setup succeeds.

## Workflows

- **Validate** runs on pull requests and is called before provisioning/deployment. It checks TypeScript, frontend tests/build, backend tests, Bicep compilation, and deployment safeguards. It needs no secrets. Successful runs publish only `dist/` as `rolloff-ready-web` for browser review, retained for three days; API, planning files and environment files are excluded.
- **Provision Azure** is manual on `main`. It creates/updates resources and secure API settings. Email defaults on and can be disabled. Disabling email clears email settings but does not delete existing email resources because deployments use incremental mode.
- **Deploy Azure** validates every main push, and deploys only when repository variable `AZURE_DEPLOY_ENABLED=true`. It also supports a manual deployment after provisioning. Azure OIDC retrieves the SWA deployment token in the job; it is masked immediately and never saved as a repository secret. The tested frontend is built to `dist`; Azure builds `api` for its managed Functions runtime. A read-only `/api/config` smoke test follows.
- **Deploy Azure** also supports rollback: supply a full 40-character commit SHA already on `main`, and type `ROLLBACK rolloff-ready`. That revision must pass current checks. Rollback restores application code, not infrastructure/settings, and does not undo already accepted emails.
- **Teardown Azure** requires `DELETE rolloff-ready`. It checks the group name, subscription, project/repository/manager tags, then rejects unrelated or untagged resources before deleting only that group. It does not delete the GitHub repository or Entra application. Set `AZURE_DEPLOY_ENABLED=false` after teardown.

The workflows share a concurrency group so deployment, provisioning, and teardown cannot overlap. OIDC jobs only run on `main` in the `production` environment; pull-request jobs receive no cloud credentials. Actions are pinned to verified repository commits, and Bicep is pinned to v0.47.16. Azure's deployment action internally uses a vendor-maintained container, so action SHA pinning does not make that underlying image immutable.

## Configuration

| Location | Values |
| --- | --- |
| GitHub `production` environment secrets | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `HERE_MAPS_API_KEY`, `APP_SECRET` |
| GitHub repository variables | `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION` (default `westus2`), `AZURE_DEPLOY_ENABLED` |
| Azure API settings, managed by Bicep | `HERE_MAPS_API_KEY`, `APP_SECRET`, `ACS_CONNECTION_STRING`, `EMAIL_SENDER`, `CONTACT_RECIPIENT`, `ALLOWED_ORIGINS`, `APP_ENV` |

The recipient is fixed to `jjfeore@gmail.com`. The allowed origin is the complete generated HTTPS SWA hostname. Custom domains require adding the exact new origin and updating the template; preview environments are disabled. App settings belong to the managed API; they are not Vite build variables. Bicep outputs only the site name, hostname, and sender address. Re-run provisioning after changing `HERE_MAPS_API_KEY` or `APP_SECRET` in GitHub. Resetting the latter invalidates existing form tokens.

## Cost and operational limits

SWA Free has quotas and no production SLA. Azure-managed email domains have sending limits; API success means provider acceptance, not confirmed inbox delivery. The basic application rate limits and duplicate suppression are process-local, so restarts and multiple instances can bypass them. Configure Azure budgets and HERE usage alerts in their portals; budgets alert rather than impose a hard spending cap. Keep the HERE account's caps and ACS default sending quotas conservative for the demo.

At the published Azure example rates checked September 16, 2026, email costs US$0.00025 per message plus US$0.00012 per MB transferred, approximately $0.25 for 1,000 small messages before data charges, taxes, or agreement-specific rates. Verify the active subscription's prices before use. No monitoring/log-storage service is provisioned; advanced distributed abuse controls are outside this demo.

Sources: [managed runtimes](https://learn.microsoft.com/en-us/azure/static-web-apps/languages-runtimes), [SWA build configuration](https://learn.microsoft.com/en-us/azure/static-web-apps/build-configuration), [SWA plans](https://learn.microsoft.com/en-us/azure/static-web-apps/plans), [Azure OIDC](https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect), [Azure-managed email domains](https://learn.microsoft.com/en-us/azure/communication-services/quickstarts/email/add-azure-managed-domains), [email pricing](https://learn.microsoft.com/en-us/azure/communication-services/concepts/email-pricing).
