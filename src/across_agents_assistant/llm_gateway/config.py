"""
LLM Gateway configuration management.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..paths import data_file


@dataclass
class ModelInfo:
    """Information about a specific model."""
    model_id: str
    name: str
    supports_vision: bool = False
    supports_function_calling: bool = False
    max_tokens: int = 8192


@dataclass
class LLMProviderConfig:
    """Configuration for an LLM provider."""
    provider_id: str
    name: str
    api_key_env: str
    endpoint: str
    provider_type: str = "openai_compatible"
    models_endpoint: Optional[str] = None
    models: List[ModelInfo] = field(default_factory=list)
    enabled: bool = True


@dataclass
class LLMConfig:
    """Global LLM Gateway configuration."""
    providers: List[LLMProviderConfig] = field(default_factory=list)
    primary_provider: str = "minimax"
    fallback_providers: List[str] = field(default_factory=list)


CONFIG_FILE = data_file("llm_config.json")
LEGACY_CONFIG_FILE = Path.home() / "Library/Application Support/AcrossAgentsAssistant/llm_config.json"


def _default_config() -> LLMConfig:
    """Return default configuration from the provider registry."""
    from .provider_registry import get_default_provider_definitions

    providers = []
    for provider in get_default_provider_definitions():
        providers.append(
            LLMProviderConfig(
                provider_id=provider.provider_id,
                name=provider.name,
                api_key_env=provider.api_key_env,
                endpoint=provider.endpoint,
                provider_type=provider.provider_type,
                models_endpoint=provider.models_endpoint,
                models=list(provider.default_models),
                enabled=provider.enabled,
            )
        )
    return LLMConfig(
        providers=providers,
        primary_provider="minimax",
        fallback_providers=["deepseek", "openai", "bailian"],
    )


def _merge_models(preferred: List[ModelInfo], saved: List[ModelInfo]) -> List[ModelInfo]:
    """Merge model lists with registry defaults first and saved custom entries preserved."""
    seen = set()
    merged: List[ModelInfo] = []
    for model in [*preferred, *saved]:
        model_id = (model.model_id or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        merged.append(model)
    return merged


def _merge_with_defaults(config: LLMConfig) -> LLMConfig:
    """Refresh persisted built-in providers against the current registry."""
    defaults = _default_config()
    saved_by_id = {provider.provider_id: provider for provider in config.providers}
    merged_providers: List[LLMProviderConfig] = []

    for default_provider in defaults.providers:
        saved = saved_by_id.pop(default_provider.provider_id, None)
        if saved is None:
            merged_providers.append(default_provider)
            continue
        merged_providers.append(
            LLMProviderConfig(
                provider_id=default_provider.provider_id,
                name=saved.name or default_provider.name,
                api_key_env=saved.api_key_env or default_provider.api_key_env,
                endpoint=saved.endpoint or default_provider.endpoint,
                provider_type=saved.provider_type or default_provider.provider_type,
                models_endpoint=saved.models_endpoint or default_provider.models_endpoint,
                models=_merge_models(default_provider.models, saved.models),
                enabled=saved.enabled,
            )
        )

    merged_providers.extend(saved_by_id.values())
    return LLMConfig(
        providers=merged_providers,
        primary_provider=config.primary_provider or defaults.primary_provider,
        fallback_providers=config.fallback_providers or defaults.fallback_providers,
    )


def _parse_config(data: Dict) -> LLMConfig:
    """Parse JSON data into LLMConfig."""
    providers = []
    for p in data.get("providers", []):
        models = [ModelInfo(**m) for m in p.get("models", [])]
        providers.append(LLMProviderConfig(
            provider_id=p["provider_id"],
            name=p["name"],
            api_key_env=p["api_key_env"],
            endpoint=p["endpoint"],
            provider_type=p.get("provider_type", p.get("type", "openai_compatible")),
            models_endpoint=p.get("models_endpoint"),
            models=models,
            enabled=p.get("enabled", True),
        ))
    return LLMConfig(
        providers=providers,
        primary_provider=data.get("primary_provider", "minimax"),
        fallback_providers=data.get("fallback_providers", []),
    )


def load_llm_config() -> LLMConfig:
    """Load LLM config from file, or return defaults if file doesn't exist."""
    source_file = CONFIG_FILE if CONFIG_FILE.exists() else LEGACY_CONFIG_FILE
    if source_file.exists():
        try:
            with open(source_file, "r") as f:
                data = json.load(f)
            if source_file == LEGACY_CONFIG_FILE and not CONFIG_FILE.exists():
                CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(CONFIG_FILE, "w") as out:
                    json.dump(data, out, indent=2)
            parsed = _parse_config(data)
            merged = _merge_with_defaults(parsed)
            if merged != parsed or source_file == LEGACY_CONFIG_FILE:
                save_llm_config(merged)
            return merged
        except (json.JSONDecodeError, KeyError):
            pass
    return _default_config()


def save_llm_config(config: LLMConfig) -> None:
    """Save LLM config to file."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "providers": [
            {
                "provider_id": p.provider_id,
                "name": p.name,
                "api_key_env": p.api_key_env,
                "endpoint": p.endpoint,
                "provider_type": p.provider_type,
                "models_endpoint": p.models_endpoint,
                "models": [vars(m) for m in p.models],
                "enabled": p.enabled,
            }
            for p in config.providers
        ],
        "primary_provider": config.primary_provider,
        "fallback_providers": config.fallback_providers,
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)
