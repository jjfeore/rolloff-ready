"""Protect deployment and teardown boundaries without contacting Azure."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud
from bootstrap_cloud import CONTRIBUTOR_ROLE, audit_project_roles, ensure_federation, production_subject, read_settings

ENV = {"AZURE_RESOURCE_GROUP": "rg-rolloff-ready", "AZURE_SUBSCRIPTION_ID": "subscription", "GITHUB_REPOSITORY": "owner/rolloff-ready"}
TAGS = {"project": "rolloff-ready", "managedBy": "rolloff-ready-bicep", "repository": "owner/rolloff-ready"}
GROUP = {"name": "rg-rolloff-ready", "id": "/subscriptions/subscription/resourceGroups/rg-rolloff-ready", "tags": TAGS}


class OidcSubjectTests(unittest.TestCase):
    repository = "owner/rolloff-ready"
    metadata = {"full_name": repository, "id": 1234, "owner": {"id": 5678}}
    prefix = "repo:owner@5678/rolloff-ready@1234"
    subject = prefix + ":environment:production"
    credential = {"name": "rolloff-ready-production", "issuer": "https://token.actions.githubusercontent.com",
                  "subject": subject, "audiences": ["api://AzureADTokenExchange"]}

    def resolve(self, settings, metadata=None):
        with patch("bootstrap_cloud.gh", side_effect=[self.metadata if metadata is None else metadata, settings]) as github:
            value = production_subject(self.repository)
            self.assertEqual(github.call_args_list, [call("api", "repos/owner/rolloff-ready"),
                                                    call("api", "repos/owner/rolloff-ready/actions/oidc/customization/sub")])
            return value

    def test_immutable_subject_uses_verified_api_prefix_and_environment(self):
        self.assertEqual(self.resolve({"use_default": True, "use_immutable_subject": True,
                                       "sub_claim_prefix": self.prefix}), self.subject)

    def test_immutable_without_prefix_uses_verified_repository_ids(self):
        self.assertEqual(self.resolve({"use_default": True, "use_immutable_subject": True}), self.subject)

    def test_legacy_default_subject_with_or_without_explicit_prefix(self):
        for settings in ({"use_default": True}, {"use_default": True, "use_immutable_subject": False,
                                                 "sub_claim_prefix": "repo:owner/rolloff-ready"}):
            self.assertEqual(self.resolve(settings), "repo:owner/rolloff-ready:environment:production")

    def test_rejects_custom_templates_without_writing_settings(self):
        for settings in ({"use_default": False, "include_claim_keys": ["repo", "context"]},
                         {"use_default": False, "include_claim_keys": ["job_workflow_ref"]}, {}, None):
            with self.subTest(settings=settings), patch("bootstrap_cloud.gh", side_effect=[self.metadata, settings]) as github:
                with self.assertRaisesRegex(RuntimeError, "Custom GitHub OIDC"):
                    production_subject(self.repository)
                self.assertTrue(all("--method" not in item.args for item in github.call_args_list))

    def test_rejects_prefix_for_wrong_repository_owner_or_numeric_ids(self):
        prefixes = ("repo:other@5678/rolloff-ready@1234", "repo:owner@5678/other@1234",
                    "repo:owner@9999/rolloff-ready@1234", "repo:owner@5678/rolloff-ready@9999",
                    "repo:owner/rolloff-ready", self.prefix + ":ref:refs/heads/main", "", None)
        for prefix in prefixes:
            with self.subTest(prefix=prefix), self.assertRaises(RuntimeError):
                self.resolve({"use_default": True, "use_immutable_subject": True, "sub_claim_prefix": prefix})

    def test_rejects_redirected_repository_or_invalid_identity_metadata(self):
        metadata_cases = ({**self.metadata, "full_name": "other/rolloff-ready"},
                          {**self.metadata, "id": "1234"}, {**self.metadata, "id": True},
                          {**self.metadata, "owner": []}, {**self.metadata, "owner": {"id": 0}})
        for metadata in metadata_cases:
            with self.subTest(metadata=metadata), self.assertRaises(RuntimeError):
                self.resolve({"use_default": True, "use_immutable_subject": True}, metadata)
        with self.assertRaises(RuntimeError):
            self.resolve({"use_default": True, "use_immutable_subject": "true"})
        with self.assertRaises(RuntimeError):
            self.resolve({"use_default": True, "use_immutable_subject": False, "sub_claim_prefix": self.prefix})

    def test_matching_federation_is_read_only(self):
        with patch("bootstrap_cloud.az", return_value=[{**self.credential, "id": "credential-id"}]) as azure:
            ensure_federation("app-id", self.repository, self.subject)
            azure.assert_called_once_with("ad", "app", "federated-credential", "list", "--id", "app-id")

    def test_creates_missing_federation_or_upgrades_only_exact_legacy_trust(self):
        legacy = {**self.credential, "id": "credential-id", "description": "Preserve this description",
                  "subject": "repo:owner/rolloff-ready:environment:production"}
        for existing, operation in (([], "create"), ([legacy], "update")):
            paths = []
            def fake_azure(*args):
                if args[3] == "list":
                    return existing
                self.assertEqual(args[3], operation)
                if operation == "update":
                    self.assertEqual(args[6:8], ("--federated-credential-id", "credential-id"))
                path = Path(args[-1][1:])
                paths.append(path)
                parameters = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(parameters["subject"], self.subject)
                for key in ("name", "issuer", "audiences"):
                    self.assertEqual(parameters[key], self.credential[key])
                if operation == "update":
                    self.assertEqual(parameters["description"], legacy["description"])
            with self.subTest(operation=operation), patch("bootstrap_cloud.az", side_effect=fake_azure) as azure:
                ensure_federation("app-id", self.repository, self.subject)
                self.assertEqual(azure.call_count, 2)
                self.assertTrue(paths)
                self.assertFalse(any(path.exists() for path in paths))

    def test_never_overwrites_unrelated_or_weaker_trust(self):
        legacy = {**self.credential, "id": "credential-id", "subject": "repo:owner/rolloff-ready:environment:production"}
        changes = ({"issuer": "https://other.example"}, {"audiences": ["other"]},
                   {"subject": "repo:other/rolloff-ready:environment:production"},
                   {"subject": "repo:owner/rolloff-ready:ref:refs/heads/main"},
                   {"subject": "repo:owner@5678/rolloff-ready@9999:environment:production"},
                   {"claimsMatchingExpression": {"value": "*"}}, {"id": None})
        for change in changes:
            with self.subTest(change=change), patch("bootstrap_cloud.az", return_value=[{**legacy, **change}]) as azure:
                with self.assertRaises(RuntimeError):
                    ensure_federation("app-id", self.repository, self.subject)
                self.assertEqual(azure.call_count, 1)
        with patch("bootstrap_cloud.az", return_value=[self.credential]) as azure:
            with self.assertRaises(RuntimeError):
                ensure_federation("app-id", self.repository, legacy["subject"])
            self.assertEqual(azure.call_count, 1)  # Never downgrade immutable to name-only.


class DeploymentSafetyTests(unittest.TestCase):
    def test_role_audit_checks_all_descendants_ancestors_and_groups(self):
        assignment = {"principalId": "principal", "scope": GROUP["id"], "roleDefinitionId": f"/providers/Microsoft.Authorization/roleDefinitions/{CONTRIBUTOR_ROLE}"}
        with patch("bootstrap_cloud.az", side_effect=[[assignment], []]) as azure:
            self.assertEqual(audit_project_roles("principal", "subscription", GROUP["id"]), [assignment])
            query = ("role", "assignment", "list", "--assignee", "principal", "--subscription", "subscription", "--include-groups", "--fill-principal-name", "false", "--fill-role-definition-name", "false")
            self.assertEqual(azure.call_args_list, [call(*query, "--all"), call(*query, "--scope", "/subscriptions/subscription", "--include-inherited")])

    def test_role_audit_rejects_broad_other_group_and_inherited_grants(self):
        for scope in ("/subscriptions/subscription", "/subscriptions/subscription/resourceGroups/other", "/providers/Microsoft.Management/managementGroups/parent"):
            with self.subTest(scope=scope), patch("bootstrap_cloud.az", return_value=[{"principalId": "principal", "scope": scope, "roleDefinitionId": CONTRIBUTOR_ROLE}]) as azure:
                with self.assertRaisesRegex(RuntimeError, "unexpected Azure role"):
                    audit_project_roles("principal", "subscription", GROUP["id"])
                self.assertEqual(azure.call_count, 2)  # Read-only queries; audit never removes a grant.

    def test_role_audit_rejects_grant_returned_only_by_ancestor_query(self):
        inherited = {"principalId": "principal", "scope": "/providers/Microsoft.Management/managementGroups/parent", "roleDefinitionId": CONTRIBUTOR_ROLE}
        with patch("bootstrap_cloud.az", side_effect=[[], [inherited]]):
            with self.assertRaises(RuntimeError):
                audit_project_roles("principal", "subscription", GROUP["id"])

    def test_role_audit_rejects_owner_or_group_grant_on_expected_group(self):
        for assignment in (
            {"principalId": "principal", "scope": GROUP["id"], "roleDefinitionId": "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"},
            {"principalId": "a-parent-group", "scope": GROUP["id"], "roleDefinitionId": CONTRIBUTOR_ROLE},
        ):
            with self.subTest(assignment=assignment), patch("bootstrap_cloud.az", return_value=[assignment]):
                with self.assertRaises(RuntimeError):
                    audit_project_roles("principal", "subscription", GROUP["id"])

    def test_role_audit_allows_empty_new_identity_but_fails_closed_on_query_error(self):
        with patch("bootstrap_cloud.az", return_value=[]):
            self.assertEqual(audit_project_roles("principal", "subscription", GROUP["id"]), [])
        with patch("bootstrap_cloud.az", side_effect=RuntimeError("Insufficient permission to audit")):
            with self.assertRaises(RuntimeError):
                audit_project_roles("principal", "subscription", GROUP["id"])

    def test_deployment_token_rejects_site_missing_manager_tag(self):
        site = {"name": "site", "tags": {"project": "rolloff-ready", "repository": "owner/rolloff-ready"}}
        with patch.dict(os.environ, ENV), patch.object(cloud, "az", side_effect=[GROUP, [site]]) as azure:
            with self.assertRaises(RuntimeError):
                cloud.deployment_token()
            self.assertFalse(any(call.args[:3] == ("staticwebapp", "secrets", "list") for call in azure.call_args_list))

    def test_wrong_confirmation_does_not_contact_azure(self):
        with patch.object(cloud, "az") as azure:
            with self.assertRaises(RuntimeError):
                cloud.teardown("delete")
            azure.assert_not_called()

    def test_rejects_group_from_other_subscription(self):
        group = {**GROUP, "id": "/subscriptions/other/resourceGroups/rg-rolloff-ready"}
        with patch.dict(os.environ, ENV), patch.object(cloud, "az", return_value=group):
            with self.assertRaises(RuntimeError):
                cloud.verified_group()

    def test_rejects_group_owned_by_another_repository(self):
        group = {**GROUP, "tags": {**TAGS, "repository": "other/repo"}}
        with patch.dict(os.environ, ENV), patch.object(cloud, "az", return_value=group):
            with self.assertRaises(RuntimeError):
                cloud.verified_group()

    def test_teardown_refuses_unrelated_resource_even_in_tagged_group(self):
        resource = {"type": "Microsoft.Storage/storageAccounts", "tags": TAGS}
        with patch.dict(os.environ, ENV), patch.object(cloud, "az", side_effect=[GROUP, [resource]]) as azure:
            with self.assertRaises(RuntimeError):
                cloud.teardown("DELETE rolloff-ready")
            self.assertFalse(any(call.args[:2] == ("group", "delete") for call in azure.call_args_list))

    def test_teardown_refuses_untagged_resources(self):
        resource = {"type": "Microsoft.Web/staticSites", "tags": {}}
        with patch.dict(os.environ, ENV), patch.object(cloud, "az", side_effect=[GROUP, [resource]]):
            with self.assertRaises(RuntimeError):
                cloud.teardown("DELETE rolloff-ready")

    def test_teardown_deletes_exact_owned_group(self):
        resource = {"type": "Microsoft.Web/staticSites", "tags": TAGS}
        with patch.dict(os.environ, ENV), patch.object(cloud, "az", side_effect=[GROUP, [resource], None]) as azure, contextlib.redirect_stdout(io.StringIO()):
            cloud.teardown("DELETE rolloff-ready")
            azure.assert_called_with("group", "delete", "--name", "rg-rolloff-ready", "--subscription", "subscription", "--yes")

    def test_provision_parameters_use_file_and_never_print_secrets(self):
        outputs = {key: {"value": value} for key, value in {"staticWebAppName": "site", "hostname": "site.azurestaticapps.net", "emailSender": "sender@example.com"}.items()}
        def fake_azure(*args):
            if args[:2] == ("group", "show"):
                return GROUP
            self.assertNotIn("private-here-key", args)
            path = Path(args[-1][1:])
            self.assertTrue(path.exists())
            self.assertIn("private-here-key", path.read_text())
            self.parameter_path = path
            return {"properties": {"outputs": outputs}}
        with patch.dict(os.environ, {**ENV, "HERE_MAPS_API_KEY": "private-here-key", "APP_SECRET": "private-app-secret" * 3}, clear=True), patch.object(cloud, "az", side_effect=fake_azure), contextlib.redirect_stdout(io.StringIO()) as output:
            cloud.provision(True)
            self.assertNotIn("private-", output.getvalue())
            self.assertFalse(self.parameter_path.exists())

    def test_environment_file_is_data_and_ignores_unrelated_secrets(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as temp:
            temp.write('HERE_MAPS_API_KEY="$(do-not-execute)"\nAZURE_CLIENT_SECRET=private\n')
            path = Path(temp.name)
        try:
            self.assertEqual(read_settings(path), {"HERE_MAPS_API_KEY": "$(do-not-execute)"})
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
