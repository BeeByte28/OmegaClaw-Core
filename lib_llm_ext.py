import os, hashlib
import openai
from typing import Optional, Tuple, Dict, Any

PROMPT_DELIMITER = ":-:-:-:"

# The MeTTa loop is single-threaded and calls the LLM inline, so a hung request
# freezes the whole agent: it stops ticking, receiving and sending. The SDK
# defaults (timeout=600s, max_retries=2) let one call block for ~30 minutes --
# a 45-minute freeze was observed in the 2026-07-16 robot logs. Fail fast
# instead; the loop retries on its next tick, and a robot voice pipeline gives
# up on a turn long before this anyway. Retries are left to the loop, since an
# SDK retry just extends the freeze.
LLM_TIMEOUT_SEC = 45.0
LLM_MAX_RETRIES = 0

from src.logger import setup_logging, get_logger


logger = get_logger(__name__)

def _log_raw(provider: str, model: str, raw: str) -> None:
    logger.debug(f"[LLM_RAW] provider={provider} model={model} chars={len(raw or '')} raw={raw!r}")

def _split_system_user(content: str) -> Tuple[str, str]:
    """
    MeTTa sends:
        <system/context> :-:-:-: <last human/wakeup message>

    Keep the split intact so providers receive a real system prompt.
    """
    if PROMPT_DELIMITER not in content:
        return "", content.strip()

    sysmsg, _, usermsg = content.partition(PROMPT_DELIMITER)
    sysmsg = sysmsg.strip()
    usermsg = usermsg.strip()

    if not usermsg:
        usermsg = "EMPTY / NO NEW USER INPUT."

    return sysmsg, usermsg

def _stable_cache_key(provider: str, model: str, sysmsg: str) -> str:
    """
    Stable key for requests sharing the same system-prefix family.
    Do not include the user message here.
    """
    marker = " LAST_SKILL_USE_RESULTS: "
    stable = sysmsg.split(marker, 1)[0].strip()
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]
    return f"{provider.lower()}:{model}:{digest}"


def _merge_dicts(base: Optional[Dict[str, Any]], extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(base or {})
    merged.update(extra or {})
    return merged

class AbstractAIProvider:
    def __init__(self, name: str):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        raise NotImplementedError

    @property
    def is_available(self) -> bool:
        raise NotImplementedError

class AIProvider(AbstractAIProvider):
    """Lazy AI provider with on-demand initialization."""

    def __init__(self, name: str, var_name: str, model_name: str, base_url: str):
        super().__init__(name)
        self._var_name = var_name
        self._model_name = model_name
        self._base_url = base_url
        self._client = None  # lazy initialization

    def _ensure_client(self):
        """Initialize client on first use."""
        if self._client is None:
            self._client = self._create_client()

    def _create_client(self) -> Optional[openai.OpenAI]:
        """Create OpenAI client from environment."""
        proxy_url = os.environ.get("GATEWAY_URL")
        if proxy_url:
            prefix = self._name.lower()
            base_url = f"{proxy_url.rstrip('/')}/{prefix}/"
            logger.info(f"[lib_llm_ext.AIProvider._create_client]: Connecting via proxy: {base_url}")
            return openai.OpenAI(
                    api_key="proxy",
                    base_url=base_url,
                    timeout=LLM_TIMEOUT_SEC,
                    max_retries=LLM_MAX_RETRIES,
                    )
        if self._var_name in os.environ:
            if self._var_name == "OLLAMA_API_KEY":
                llm_server_local_url = os.environ.get("LLM_SERVER_LOCAL_URL")
                if llm_server_local_url:
                    self._base_url = llm_server_local_url.rstrip("/") + "/v1"
                elif not self._base_url.endswith("/v1"):
                    self._base_url = self._base_url.rstrip("/") + "/v1"

            return openai.OpenAI(
                api_key=os.environ.get(self._var_name),
                base_url=self._base_url,
                timeout=LLM_TIMEOUT_SEC,
                max_retries=LLM_MAX_RETRIES,
            )

        return None

    @property
    def is_available(self) -> bool:
        """Check if provider is configured (without initializing)."""
        return bool(os.environ.get("GATEWAY_URL")) or bool(os.environ.get(self._var_name))

    def _build_messages(self, content: str):
        sysmsg, usermsg = _split_system_user(content)

        if sysmsg:
            return [
                {"role": "system", "content": sysmsg},
                {"role": "user", "content": usermsg},
            ]

        return [{"role": "user", "content": usermsg}]

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        """Send chat request, initializing client if needed."""
        self._ensure_client()

        if self._client is None:
            raise RuntimeError(f"{self.name} not configured (set {self._var_name})")

        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=self._build_messages(content),
                max_tokens=max_tokens,
                **kwargs
            )

            raw = response.choices[0].message.content or ""
            _log_raw(self._name, self._model_name, raw)
            resp = self._clean_text(raw)
            return resp
        except Exception as e:
            logger.exception(f"[lib_llm_ext.AIProvider.chat]: Exception while communicating with LLM: {e}")
            return ""

    def _clean_text(self, text: str) -> str:
        """Unescape special characters."""
        return text.replace("_quote_", '"').replace("_apostrophe_", "'").replace("</arg_value>", " ") \
                    .replace("</tool_call>", " ").replace("<arg_value>", " ").replace("<tool_call>", " ")

class OpenRouterProvider(AIProvider):
    """OpenRouter provider with reasoning mode enabled (reasoning tokens excluded from the response)."""

    def _create_client(self) -> Optional[openai.OpenAI]:
        """Create OpenRouter client from environment."""
        proxy_url = os.environ.get("GATEWAY_URL")
        if proxy_url:
            base_url = f"{proxy_url.rstrip('/')}/openrouter/"
            logger.info(f"[lib_llm_ext.OpenRouterProvider._create_client]: Connecting via proxy: {base_url}")
            return openai.OpenAI(
                    api_key="proxy",
                    base_url=base_url,
                    timeout=LLM_TIMEOUT_SEC,
                    max_retries=LLM_MAX_RETRIES,
                    )
        if self._var_name in os.environ:
            return openai.OpenAI(
                api_key=os.environ.get(self._var_name),
                base_url=self._base_url,
                timeout=LLM_TIMEOUT_SEC,
                max_retries=LLM_MAX_RETRIES,
            )

        return None
    
    def _openrouter_extra_body(self, content: str, max_tokens: int) -> Dict[str, Any]:
        sysmsg, _ = _split_system_user(content)

        body = {
            "reasoning": {
                "enabled": True,
                "max_tokens": max_tokens,
                "exclude": True,
            }
        }

        # Helps OpenRouter sticky-route requests for better cache locality.
        # Keep this stable per agent/session.
        session_id = os.environ.get("OPENROUTER_SESSION_ID")
        if not session_id and sysmsg:
            session_id = _stable_cache_key("openrouter", self._model_name, sysmsg)

        if session_id:
            body["session_id"] = session_id[:256]

        model = self._model_name.lower()

        # OpenRouter supports top-level cache_control for Anthropic Claude routes.
        if model.startswith("anthropic/"):
            body["cache_control"] = {
                "type": "ephemeral",
                "ttl": os.environ.get("OPENROUTER_CACHE_TTL", "5m"),
            }

        return body


    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        extra_body = _merge_dicts(
            self._openrouter_extra_body(content, max_tokens),
            kwargs.pop("extra_body", None),
        )

        return super().chat(
            content=content,
            max_tokens=max_tokens,
            reasoning=reasoning,
            extra_body=extra_body,
            **kwargs,
        )

class AsiOneProvider(AIProvider):
    """Lazy AI provider with on-demand initialization."""

    def __init__(self, name: str, var_name: str, model_name: str, base_url: str):
        super().__init__(name, var_name, model_name, base_url)

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        """Send chat request, initializing client if needed."""
        self._ensure_client()

        if self._client is None:
            raise RuntimeError(f"{self.name} not configured (set {self._var_name})")

        sysmsg, usermsg = content.split(":-:-:-:")
        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=[{"role": "system", "content": sysmsg},
                          {"role": "user", "content": usermsg}],
                max_tokens=max_tokens,
                extra_body={
                    "enable_thinking": True,
                    "thinking_budget": 6000 
                },
                **kwargs
            )

            raw = response.choices[0].message.content
            _log_raw(self._name, self._model_name, raw)
            resp = self._clean_text(raw)
            return resp
        except Exception as e:
            logger.exception(f"[lib_llm_ext.ASIOneProvider.chat]: Exception while communicating with LLM: {e}")
            return ""


class OpenAIProvider(AIProvider):
    """OpenAI provider using the Responses API (reasoning models)."""

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        """Send chat request via the Responses API, initializing client if needed."""
        self._ensure_client()

        if self._client is None:
            raise RuntimeError(f"{self.name} not configured (set {self._var_name})")

        sysmsg, usermsg = _split_system_user(content)

        try:
            create_kwargs = {
                "instructions": sysmsg,
                "model": self._model_name,
                "input": usermsg,
                "max_output_tokens": max_tokens,
                "reasoning": {"effort": reasoning},
                "prompt_cache_key": os.environ.get("OPENAI_PROMPT_CACHE_KEY", _stable_cache_key("openai", self._model_name, sysmsg)),
            }
            # GPT-5.5 supports only 24h; GPT-5.4 also supports extended retention.
            if self._model_name.startswith(("gpt-5.5", "gpt-5.4")):
                create_kwargs["prompt_cache_retention"] = "24h"

            create_kwargs.update(kwargs)

            response = self._client.responses.create(**create_kwargs)

            usage = getattr(response, "usage", None)
            if usage:
                input_tokens = getattr(usage, "input_tokens", None)
                output_tokens = getattr(usage, "output_tokens", None)
                total_tokens = getattr(usage, "total_tokens", None)
                details = getattr(usage, "input_tokens_details", None)
                cached_tokens = getattr(details, "cached_tokens", None) if details else None

                print(
                    f"[LLM_USAGE] provider={self._name} model={self._model_name} "
                    f"input_tokens={input_tokens} output_tokens={output_tokens} "
                    f"total_tokens={total_tokens} cached_tokens={cached_tokens}"
                )

            raw = response.output_text or ""
            _log_raw(self._name, self._model_name, raw)
            return self._clean_text(raw)
        except Exception as e:
            logger.exception(f"[lib_llm_ext.OpenAIProvider.chat]: Exception while communicating with LLM: {e}")
            return ""


class TestProvider(AbstractAIProvider):
    """Test provider for mocking LLM output"""

    def __init__(self):
        super().__init__("Test")
        self._mock = None
        self._controller_ip = os.environ.get("TEST_SERVER_IP")

    def _llm_mock(self):
        if not self._mock:
            from Autotests.mock.llm import LlmMockAgent, LLM_MOCK_PORT
            self._mock = LlmMockAgent((self._controller_ip, LLM_MOCK_PORT))
        return self._mock

    @property
    def is_available(self) -> bool:
        return self._controller_ip is not None

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        return self._llm_mock().chat(content)

# Provider registry - lazy, no initialization yet
_provider_registry = {}


def _register_provider(name: str, var_name: str, model_name: str, base_url: str):
    """Register a provider configuration (no instantiation yet)."""
    _register_provider_instance(AIProvider(name, var_name, model_name, base_url))

def _register_provider_instance(provider: AbstractAIProvider):
    """Register a pre-initialized provider configuration (no instantiation yet)."""
    _provider_registry[provider.name] = provider

def _get_provider(name: str) -> Optional[AIProvider]:
    """Get or create provider instance on demand."""
    return _provider_registry.get(name)


# Register all providers (cheap - just stores config)
_register_provider(name="ASICloud", var_name="ASI_API_KEY", model_name="minimax/minimax-m3", base_url="https://inference.asicloud.cudos.org/v1")
_register_provider(name="Anthropic", var_name="ANTHROPIC_API_KEY", model_name="claude-opus-4-8", base_url="https://api.anthropic.com/v1/")
_register_provider(name="Ollama-local", var_name="OLLAMA_API_KEY", model_name="qwen3.5:9b", base_url="http://localhost:11434/v1")
_register_provider_instance(AsiOneProvider(name="ASIOne", var_name="ASIONE_API_KEY", model_name="asi1-ultra", base_url="https://api.asi1.ai/v1"))
_register_provider_instance(OpenRouterProvider(name="OpenRouter", var_name="OPENROUTER_API_KEY", model_name="z-ai/glm-5.2", base_url="https://openrouter.ai/api/v1"))
_register_provider_instance(OpenRouterProvider(name="MiniMaxM3", var_name="OPENROUTER_API_KEY", model_name="minimax/minimax-m3", base_url="https://openrouter.ai/api/v1"))
_register_provider_instance(TestProvider())
_register_provider_instance(OpenAIProvider(name="OpenAI", var_name="OPENAI_API_KEY", model_name="gpt-5.5", base_url="https://api.openai.com/v1"))


def callProvider(provider_name: str, content: str, max_tokens: int = 6000, reasoning: str = "medium") -> str:
    """Generic dispatcher for MeTTa."""
    provider = _get_provider(provider_name)
    if not provider or not provider.is_available:
        raise RuntimeError(f"Provider '{provider_name}' not available")
    return provider.chat(content=content, max_tokens=max_tokens, reasoning=reasoning)

_embedding_model = None

def initLocalEmbedding():
    model_name="intfloat/e5-large-v2"
    global _embedding_model
    os.environ["HF_HUB_OFFLINE"] = "1"
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(model_name)
    return _embedding_model

def useLocalEmbedding(atom):
    global _embedding_model
    if _embedding_model is None:
        raise RuntimeError("Call initLocalEmbedding() first.")
    return _embedding_model.encode(
        atom,
        normalize_embeddings=True
    ).tolist()


