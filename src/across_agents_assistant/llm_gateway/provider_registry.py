"""Cloud LLM provider registry.

The registry keeps public, non-secret provider metadata in one place so the
settings UI, credential store, gateway, and readiness checks agree on which
providers are supported by this build.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import ModelInfo


@dataclass(frozen=True)
class ProviderDefinition:
    provider_id: str
    name: str
    api_key_env: str
    endpoint: str
    provider_type: str = "openai_compatible"
    models_endpoint: Optional[str] = None
    default_models: tuple[ModelInfo, ...] = field(default_factory=tuple)
    enabled: bool = True


DEFAULT_PROVIDER_DEFINITIONS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        provider_id="openai",
        name="OpenAI",
        api_key_env="OPENAI_API_KEY",
        endpoint="https://api.openai.com/v1",
        models_endpoint="https://api.openai.com/v1/models",
        default_models=(
            ModelInfo("gpt-5.5", "GPT-5.5", supports_function_calling=True, max_tokens=8192),
            ModelInfo("gpt-5.4", "GPT-5.4", supports_function_calling=True, max_tokens=8192),
            ModelInfo("gpt-5.4-mini", "GPT-5.4 Mini", supports_function_calling=True, max_tokens=8192),
            ModelInfo("gpt-5.4-nano", "GPT-5.4 Nano", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="anthropic",
        name="Anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        endpoint="https://api.anthropic.com/v1",
        provider_type="anthropic",
        models_endpoint="https://api.anthropic.com/v1/models",
        default_models=(
            ModelInfo("claude-opus-4-8", "Claude Opus 4.8", supports_function_calling=True, max_tokens=8192),
            ModelInfo("claude-sonnet-4-6", "Claude Sonnet 4.6", supports_function_calling=True, max_tokens=8192),
            ModelInfo("claude-haiku-4-5-20251001", "Claude Haiku 4.5", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="deepseek",
        name="DeepSeek",
        api_key_env="DEEPSEEK_API_KEY",
        endpoint="https://api.deepseek.com/v1",
        models_endpoint="https://api.deepseek.com/v1/models",
        default_models=(
            ModelInfo("deepseek-v4-pro", "DeepSeek V4 Pro", supports_function_calling=True, max_tokens=8192),
            ModelInfo("deepseek-v4-flash", "DeepSeek V4 Flash", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="minimax",
        name="MiniMax",
        api_key_env="MINIMAX_API_KEY",
        endpoint="https://api.minimaxi.com/v1",
        models_endpoint="https://api.minimaxi.com/v1/models",
        default_models=(
            ModelInfo("MiniMax-M3", "MiniMax M3", supports_function_calling=True, max_tokens=8192),
            ModelInfo("MiniMax-M2.7", "MiniMax M2.7", supports_function_calling=True, max_tokens=8192),
            ModelInfo("MiniMax-M2.7-highspeed", "MiniMax M2.7 High Speed", supports_function_calling=True, max_tokens=8192),
            ModelInfo("MiniMax-M2.5", "MiniMax M2.5", supports_function_calling=True, max_tokens=8192),
            ModelInfo("MiniMax-M2.5-highspeed", "MiniMax M2.5 High Speed", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="bailian",
        name="Alibaba Bailian / Qwen",
        api_key_env="BAILIAN_API_KEY",
        endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models_endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        default_models=(
            ModelInfo("qwen3.7-max", "Qwen 3.7 Max", supports_function_calling=True, max_tokens=8192),
            ModelInfo("qwen3.7-plus", "Qwen 3.7 Plus", supports_function_calling=True, max_tokens=8192),
            ModelInfo("qwen3.6-flash", "Qwen 3.6 Flash", supports_function_calling=True, max_tokens=8192),
            ModelInfo("qwen-plus-latest", "Qwen Plus Latest", supports_function_calling=True, max_tokens=8192),
            ModelInfo("qwen-max-latest", "Qwen Max Latest", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="moonshot",
        name="Moonshot / Kimi",
        api_key_env="MOONSHOT_API_KEY",
        endpoint="https://api.moonshot.ai/v1",
        models_endpoint="https://api.moonshot.ai/v1/models",
        default_models=(
            ModelInfo("kimi-k2.6", "Kimi K2.6", supports_function_calling=True, max_tokens=8192),
            ModelInfo("kimi-k2-thinking-turbo", "Kimi K2 Thinking Turbo", supports_function_calling=True, max_tokens=8192),
            ModelInfo("moonshot-v1-128k", "Moonshot v1 128K", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="zhipu",
        name="Zhipu GLM",
        api_key_env="ZHIPU_API_KEY",
        endpoint="https://open.bigmodel.cn/api/paas/v4",
        models_endpoint="https://open.bigmodel.cn/api/paas/v4/models",
        default_models=(
            ModelInfo("glm-5.1", "GLM-5.1", supports_function_calling=True, max_tokens=8192),
            ModelInfo("glm-5", "GLM-5", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="volcengine",
        name="Volcengine Ark / Doubao",
        api_key_env="VOLCENGINE_API_KEY",
        endpoint="https://ark.cn-beijing.volces.com/api/v3",
        models_endpoint="https://ark.cn-beijing.volces.com/api/v3/models",
        default_models=(
            ModelInfo("doubao-seed-2.0-mini", "Doubao Seed 2.0 Mini", supports_function_calling=True, max_tokens=8192),
            ModelInfo("doubao-seed-1-8-251228", "Doubao Seed 1.8", supports_function_calling=True, max_tokens=8192),
            ModelInfo("doubao-seed-1-6-thinking-250715", "Doubao Seed 1.6 Thinking", supports_function_calling=True, max_tokens=8192),
            ModelInfo("doubao-seed-1-6-flash-250828", "Doubao Seed 1.6 Flash", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="google",
        name="Google Gemini",
        api_key_env="GEMINI_API_KEY",
        endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
        models_endpoint="https://generativelanguage.googleapis.com/v1beta/openai/models",
        default_models=(
            ModelInfo("gemini-3.1-pro", "Gemini 3.1 Pro", supports_function_calling=True, max_tokens=8192),
            ModelInfo("gemini-3.5-flash", "Gemini 3.5 Flash", supports_function_calling=True, max_tokens=8192),
            ModelInfo("gemini-3-flash", "Gemini 3 Flash", supports_function_calling=True, max_tokens=8192),
            ModelInfo("gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="xai",
        name="xAI",
        api_key_env="XAI_API_KEY",
        endpoint="https://api.x.ai/v1",
        models_endpoint="https://api.x.ai/v1/models",
        default_models=(
            ModelInfo("grok-4.3", "Grok 4.3", supports_function_calling=True, max_tokens=8192),
            ModelInfo("grok-4.3-latest", "Grok 4.3 Latest", supports_function_calling=True, max_tokens=8192),
            ModelInfo("grok-build-0.1", "Grok Build 0.1", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="mistral",
        name="Mistral AI",
        api_key_env="MISTRAL_API_KEY",
        endpoint="https://api.mistral.ai/v1",
        models_endpoint="https://api.mistral.ai/v1/models",
        default_models=(
            ModelInfo("mistral-large-latest", "Mistral Large", supports_function_calling=True, max_tokens=8192),
            ModelInfo("mistral-medium-latest", "Mistral Medium", supports_function_calling=True, max_tokens=8192),
            ModelInfo("magistral-medium-latest", "Magistral Medium", supports_function_calling=True, max_tokens=8192),
            ModelInfo("devstral-latest", "Devstral", supports_function_calling=True, max_tokens=8192),
            ModelInfo("codestral-latest", "Codestral", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="groq",
        name="Groq",
        api_key_env="GROQ_API_KEY",
        endpoint="https://api.groq.com/openai/v1",
        models_endpoint="https://api.groq.com/openai/v1/models",
        default_models=(
            ModelInfo("openai/gpt-oss-120b", "GPT OSS 120B", supports_function_calling=True, max_tokens=8192),
            ModelInfo("llama-3.3-70b-versatile", "Llama 3.3 70B Versatile", supports_function_calling=True, max_tokens=8192),
            ModelInfo("groq/compound", "Groq Compound", supports_function_calling=True, max_tokens=8192),
            ModelInfo("openai/gpt-oss-20b", "GPT OSS 20B", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="cohere",
        name="Cohere",
        api_key_env="COHERE_API_KEY",
        endpoint="https://api.cohere.com/compatibility/v1",
        models_endpoint="https://api.cohere.com/compatibility/v1/models",
        default_models=(
            ModelInfo("command-a-plus-05-2026", "Command A+", supports_function_calling=True, max_tokens=8192),
            ModelInfo("command-a-reasoning-08-2025", "Command A Reasoning", supports_function_calling=True, max_tokens=8192),
            ModelInfo("command-a-vision-07-2025", "Command A Vision", supports_function_calling=True, max_tokens=8192),
            ModelInfo("command-a-03-2025", "Command A", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="openrouter",
        name="OpenRouter",
        api_key_env="OPENROUTER_API_KEY",
        endpoint="https://openrouter.ai/api/v1",
        models_endpoint="https://openrouter.ai/api/v1/models",
        default_models=(
            ModelInfo("openrouter/auto", "OpenRouter Auto", supports_function_calling=True, max_tokens=8192),
            ModelInfo("anthropic/claude-sonnet-4.5", "Claude Sonnet 4.5", supports_function_calling=True, max_tokens=8192),
            ModelInfo("openai/gpt-5", "GPT-5", supports_function_calling=True, max_tokens=8192),
            ModelInfo("google/gemini-2.5-pro", "Gemini 2.5 Pro", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="together",
        name="Together AI",
        api_key_env="TOGETHER_API_KEY",
        endpoint="https://api.together.ai/v1",
        models_endpoint="https://api.together.ai/v1/models",
        default_models=(
            ModelInfo("openai/gpt-oss-120b", "GPT OSS 120B", supports_function_calling=True, max_tokens=8192),
            ModelInfo("openai/gpt-oss-20b", "GPT OSS 20B", supports_function_calling=True, max_tokens=8192),
            ModelInfo("zai-org/GLM-5", "GLM-5", supports_function_calling=True, max_tokens=8192),
            ModelInfo("deepseek-ai/DeepSeek-V3.1", "DeepSeek V3.1", supports_function_calling=True, max_tokens=8192),
        ),
    ),
    ProviderDefinition(
        provider_id="fireworks",
        name="Fireworks AI",
        api_key_env="FIREWORKS_API_KEY",
        endpoint="https://api.fireworks.ai/inference/v1",
        models_endpoint="https://api.fireworks.ai/inference/v1/models",
        default_models=(
            ModelInfo("accounts/fireworks/models/kimi-k2p5", "Kimi K2.5", supports_function_calling=True, max_tokens=8192),
            ModelInfo("accounts/fireworks/models/llama-v3p1-70b-instruct", "Llama 3.1 70B Instruct", supports_function_calling=True, max_tokens=8192),
            ModelInfo("accounts/fireworks/models/deepseek-v3", "DeepSeek V3", supports_function_calling=True, max_tokens=8192),
        ),
    ),
)


def get_default_provider_definitions() -> tuple[ProviderDefinition, ...]:
    return DEFAULT_PROVIDER_DEFINITIONS


def get_default_provider_ids() -> tuple[str, ...]:
    return tuple(provider.provider_id for provider in DEFAULT_PROVIDER_DEFINITIONS)


def get_provider_definition(provider_id: str) -> Optional[ProviderDefinition]:
    for provider in DEFAULT_PROVIDER_DEFINITIONS:
        if provider.provider_id == provider_id:
            return provider
    return None
