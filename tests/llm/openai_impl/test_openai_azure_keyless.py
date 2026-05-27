"""Tests for Azure OpenAI keyless auth via DefaultAzureCredential."""

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


def _reload_openai_module():
    """Force reimport so module-level state doesn't bleed between tests."""
    mod_name = "madrag.llm.openai"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    return importlib.import_module(mod_name)


class TestAzureKeylessAuth:
    def test_api_key_path_skips_credential(self, monkeypatch):
        """Explicit API key → AsyncAzureOpenAI receives api_key, no DefaultAzureCredential."""
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("LLM_BINDING_API_KEY", raising=False)

        mock_client_cls = MagicMock()

        with patch("openai.AsyncAzureOpenAI", mock_client_cls):
            from madrag.llm.openai import create_openai_async_client

            create_openai_async_client(
                api_key="sk-test",
                use_azure=True,
                base_url="https://myresource.openai.azure.com/",
                api_version="2024-08-01-preview",
            )

        call_kwargs = mock_client_cls.call_args.kwargs
        assert call_kwargs.get("api_key") == "sk-test"
        assert "azure_ad_token_provider" not in call_kwargs

    def test_env_api_key_path_skips_credential(self, monkeypatch):
        """AZURE_OPENAI_API_KEY env var → api_key path, no DefaultAzureCredential."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "env-key-123")

        mock_client_cls = MagicMock()

        with patch("openai.AsyncAzureOpenAI", mock_client_cls):
            from madrag.llm.openai import create_openai_async_client

            create_openai_async_client(
                use_azure=True,
                base_url="https://myresource.openai.azure.com/",
                api_version="2024-08-01-preview",
            )

        call_kwargs = mock_client_cls.call_args.kwargs
        assert call_kwargs.get("api_key") == "env-key-123"
        assert "azure_ad_token_provider" not in call_kwargs

    def test_no_api_key_uses_token_provider(self, monkeypatch):
        """No API key → azure_ad_token_provider injected, api_key absent."""
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("LLM_BINDING_API_KEY", raising=False)

        fake_token_provider = MagicMock(return_value="bearer-token")
        mock_credential = MagicMock()
        mock_client_cls = MagicMock()

        with (
            patch("openai.AsyncAzureOpenAI", mock_client_cls),
            patch(
                "azure.identity.DefaultAzureCredential",
                return_value=mock_credential,
            ),
            patch(
                "azure.identity.get_bearer_token_provider",
                return_value=fake_token_provider,
            ),
        ):
            from madrag.llm.openai import create_openai_async_client

            create_openai_async_client(
                use_azure=True,
                base_url="https://myresource.openai.azure.com/",
                api_version="2024-08-01-preview",
            )

        call_kwargs = mock_client_cls.call_args.kwargs
        assert "api_key" not in call_kwargs
        assert call_kwargs.get("azure_ad_token_provider") is fake_token_provider

    def test_missing_azure_identity_raises_import_error(self, monkeypatch):
        """azure-identity not installed → ImportError with install hint."""
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("LLM_BINDING_API_KEY", raising=False)

        mock_client_cls = MagicMock()

        # Simulate azure.identity not being installed
        with (
            patch("openai.AsyncAzureOpenAI", mock_client_cls),
            patch.dict(sys.modules, {"azure.identity": None}),
        ):
            from madrag.llm.openai import create_openai_async_client

            with pytest.raises(ImportError, match="azure-identity"):
                create_openai_async_client(
                    use_azure=True,
                    base_url="https://myresource.openai.azure.com/",
                    api_version="2024-08-01-preview",
                )

    def test_llm_binding_api_key_env_path(self, monkeypatch):
        """LLM_BINDING_API_KEY (not AZURE_OPENAI_API_KEY) → api_key path."""
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("LLM_BINDING_API_KEY", "binding-key-456")

        mock_client_cls = MagicMock()

        with patch("openai.AsyncAzureOpenAI", mock_client_cls):
            from madrag.llm.openai import create_openai_async_client

            create_openai_async_client(
                use_azure=True,
                base_url="https://myresource.openai.azure.com/",
                api_version="2024-08-01-preview",
            )

        call_kwargs = mock_client_cls.call_args.kwargs
        assert call_kwargs.get("api_key") == "binding-key-456"
        assert "azure_ad_token_provider" not in call_kwargs
