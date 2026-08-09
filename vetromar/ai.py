"""AI access resolution: bring-your-own provider.

The ONE place client construction happens. Two provider types
(config.ai_provider): native Anthropic (ANTHROPIC_API_KEY / credentials file)
and any OpenAI-compatible endpoint (base URL + optional key). Keys are the
user's own — nothing routes through a Vetromar service.
"""

from __future__ import annotations

from vetromar.errors import ConfigError

API_KEY_HINT = (
    "Add an AI provider in Settings (an Anthropic API key, or any "
    "OpenAI-compatible endpoint), or set ANTHROPIC_API_KEY."
)


def ai_available(config) -> bool:
    if getattr(config, "ai_provider", "anthropic") == "openai":
        return bool(config.openai_base_url)
    return bool(config.api_key)


def get_provider(config):
    """The one place a provider implementation is chosen (config.ai_provider:
    "anthropic" native, or "openai" for any OpenAI-compatible endpoint).
    Raises ConfigError when the selected provider isn't configured."""
    if getattr(config, "ai_provider", "anthropic") == "openai":
        from vetromar.providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(config)
    from vetromar.providers.anthropic import AnthropicProvider

    return AnthropicProvider(config)


def anthropic_client(config):
    """The single Anthropic constructor (native provider only)."""
    import anthropic

    if config.api_key:
        return anthropic.Anthropic(api_key=config.api_key)
    raise ConfigError(
        "No Anthropic API key configured.",
        hint=API_KEY_HINT,
    )


def map_ai_error(exc, config) -> ConfigError | None:
    """Translate a provider SDK error into a friendly ConfigError, or None
    when it isn't ours to translate (caller re-raises the original).
    Delegates to the active provider — each SDK has its own error types."""
    try:
        provider = get_provider(config)
    except ConfigError:
        return None
    return provider.map_error(exc)


def deepgram_target(config) -> tuple[str, dict] | None:
    """(listen_url, auth headers) for cloud transcription, from the user's own
    Deepgram key. None when no key is configured (the local tier applies)."""
    from vetromar.transcription.deepgram import DEEPGRAM_LISTEN_URL

    if config.deepgram_api_key:
        return DEEPGRAM_LISTEN_URL, {"Authorization": f"Token {config.deepgram_api_key}"}
    return None
