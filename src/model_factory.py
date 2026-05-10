"""
Model Factory - Create model objects based on provider configuration.

This module provides a factory pattern to instantiate model objects
for different LLM providers (Google Gemini, vLLM, etc.) based on
configuration parameters.
"""

from typing import Any, Optional, Union
from declarative_agent_sdk.constants import DEFAULT_MODEL
from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


class ModelFactory:
    """Factory class to create model objects for different LLM providers."""
    
    @staticmethod
    def create_model(
        model_name: str = DEFAULT_MODEL,
        provider: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        **kwargs
    ) -> Union[str, Any]:
        """
        Create a model object based on the provider configuration.
        
        Args:
            model_name: Name of the model (e.g., "gemini-2.0-flash-exp", "Qwen/Qwen3-4B")
            provider: LLM provider ("google", "vllm", or None for default)
            endpoint_url: API endpoint URL (required for vLLM)
            **kwargs: Additional provider-specific configuration
            
        Returns:
            Model object or model name string depending on provider
            
        Raises:
            ValueError: If required parameters are missing for the provider
            
        Examples:
            # Google Gemini (default)
            model = ModelFactory.create_model("gemini-2.0-flash-exp")
            
            # vLLM with local endpoint
            model = ModelFactory.create_model(
                model_name="Qwen/Qwen3-4B",
                provider="vllm",
                endpoint_url="http://localhost:8000/v1"
            )
        """
        if provider is None or provider.lower() == "google":
            # Google Gemini - return model name as-is
            logger.info(f"Creating Google Gemini model: {model_name}")
            return model_name
        
        elif provider.lower() == "vllm":
            # vLLM via LiteLLM - create LiteLLM wrapper
            if not endpoint_url:
                raise ValueError("endpoint_url is required for vLLM provider")
            
            logger.info(f"Creating vLLM model: {model_name} at {endpoint_url}")
            return ModelFactory._create_vllm_model(model_name, endpoint_url, **kwargs)
        elif provider.lower() == "openai":
            # OpenAI-compatible model via LiteLLM
            logger.info(f"Creating OpenAI-compatible model: {model_name}")
            return ModelFactory._create_openai_model(model_name)
        
        else:
            raise ValueError(f"Unsupported provider: {provider}. Supported providers: google, vllm, openai")
    
    @staticmethod
    def _create_openai_model(model_name: str) -> Any:
        """
        Create an OpenAI model configuration.
        
        Args:
            model_name: Name of the OpenAI model
            
        Returns:
            Model name string
        """
        try:
            from google.adk.models.lite_llm import LiteLlm
            import os
            
            # Check OPENAI_API_KEY for OpenAI-compatible models
            if "OPENAI_API_KEY" not in os.environ:
                raise ValueError("OPENAI_API_KEY environment variable is required for OpenAI-compatible models")
            
            # LiteLLM requires openai/ prefix for OpenAI-compatible endpoints
            prefixed_model = f"openai/{model_name}"
            
            # Create a simple model configuration object
            openai_model = LiteLlm(
                model=prefixed_model
            )
            logger.info(f"Created OpenAI-compatible model config: {prefixed_model}")
            return openai_model
            
        except ImportError:
            raise ImportError(
                "google.adk.models.lite_llm.LiteLlm is not available. Please install litellm to use OpenAI provider: pip install litellm"
            )

    @staticmethod
    def _create_vllm_model(model_name: str, endpoint_url: str, **kwargs) -> Any:
        """
        Create a vLLM model using LiteLLM.
        
        Args:
            model_name: Model name served by vLLM
            endpoint_url: vLLM server endpoint URL
            **kwargs: Additional LiteLLM configuration (temperature, max_tokens, etc.)
            
        Returns:
            LiteLLM model configuration object
        """
        try:
            from google.adk.models.lite_llm import LiteLlm
            import os
            
            # Set dummy API key for local vLLM servers
            if "OPENAI_API_KEY" not in os.environ:
                os.environ["OPENAI_API_KEY"] = "sk-dummy-key"
                logger.debug("Set dummy OPENAI_API_KEY for vLLM")
            
            # LiteLLM requires openai/ prefix for OpenAI-compatible endpoints
            prefixed_model = f"openai/{model_name}"
            
            # Create a simple model configuration object
            vllm_model = LiteLlm(
                model=prefixed_model,
                api_base=endpoint_url,
                **kwargs
            )
            
            logger.info(f"Created LiteLLM vLLM config: {prefixed_model} @ {endpoint_url}")
            return vllm_model
            
        except ImportError:
            raise ImportError(
                "google.adk.models.lite_llm.LiteLlm is not available. Please install litellm to use vLLM provider: pip install litellm"
            )
