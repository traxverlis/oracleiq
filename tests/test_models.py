import unittest
from unittest.mock import Mock, patch

from analyzer import copilot_client


class ModelCatalogTests(unittest.TestCase):
    def test_catalog_filters_incompatible_models_and_deduplicates(self):
        response = Mock(status_code=200)
        response.json.return_value = {"data": [
            {"id": "chat-new", "name": "New model", "vendor": "Vendor", "capabilities": {"type": "chat"}},
            {"id": "chat-new", "name": "New model", "vendor": "Vendor"},
            {"id": "embedding", "capabilities": {"type": "embeddings"}},
            {"id": "disabled", "policy": {"state": "disabled"}},
            {"id": "hidden", "model_picker_enabled": False},
            {"id": "responses-only", "supported_endpoints": ["/responses"]},
            {"id": None},
        ]}
        with patch.object(copilot_client, "get_copilot_token", return_value="fixture") as token:
            with patch.object(copilot_client.requests, "get", return_value=response) as get:
                self.assertEqual(copilot_client.list_account_models(), [{"id": "chat-new", "name": "New model", "vendor": "Vendor"}])
                token.assert_called_once_with(interactive=False, force_refresh=False)
                self.assertEqual(get.call_args.kwargs["timeout"], 30)

    def test_missing_auth_never_starts_device_flow(self):
        with patch.object(copilot_client, "_load_cache", return_value={}):
            with patch.object(copilot_client, "get_oauth_token") as oauth:
                with self.assertRaises(RuntimeError):
                    copilot_client.get_copilot_token(interactive=False)
                oauth.assert_not_called()

    def test_unauthorized_token_is_refreshed_once(self):
        expired = Mock(status_code=401)
        success = Mock(status_code=200)
        success.json.return_value = {"data": []}
        with patch.object(copilot_client, "get_copilot_token", return_value="fixture") as token:
            with patch.object(copilot_client.requests, "get", side_effect=[expired, success]):
                self.assertEqual(copilot_client.list_account_models(), [])
                self.assertEqual(token.call_count, 2)
                token.assert_called_with(interactive=False, force_refresh=True)

    def test_malformed_catalog_is_not_an_empty_success(self):
        response = Mock(status_code=200)
        response.json.return_value = {"message": "not a catalog"}
        with patch.object(copilot_client, "get_copilot_token", return_value="fixture"):
            with patch.object(copilot_client.requests, "get", return_value=response):
                with self.assertRaises(ValueError):
                    copilot_client.list_account_models()