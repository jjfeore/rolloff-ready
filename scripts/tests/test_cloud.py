"""Protect deployment and teardown boundaries without contacting Azure."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud
from bootstrap_cloud import CONTRIBUTOR_ROLE, audit_project_roles, read_settings

ENV = {"AZURE_RESOURCE_GROUP": "rg-rolloff-ready", "AZURE_SUBSCRIPTION_ID": "subscription", "GITHUB_REPOSITORY": "owner/rolloff-ready"}
TAGS = {"project": "rolloff-ready", "managedBy": "rolloff-ready-bicep", "repository": "owner/rolloff-ready"}
GROUP = {"name": "rg-rolloff-ready", "id": "/subscriptions/subscription/resourceGroups/rg-rolloff-ready", "tags": TAGS}


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
