import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
import httpx

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

logger = logging.getLogger(__name__)

PROVIDER_DEFAULTS = {
    "openai": {
        "model": "gpt-4o",
        "endpoint": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
    },
    "anthropic": {
        "model": "claude-sonnet-4-20250514",
        "endpoint": "https://api.anthropic.com",
        "env_key": "ANTHROPIC_API_KEY",
    },
    "ollama": {
        "model": "llama3.2-vision",
        "endpoint": "http://localhost:11434",
        "env_key": None,
    },
    "groq": {
        "model": "llama-3.2-90b-vision",
        "endpoint": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
    },
    "openrouter": {
        "model": "openai/gpt-4o",
        "endpoint": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
    },
}


class LLMError(Exception):
    pass


def get_default_provider():
    return os.environ.get("JVCAPTURE_LLM_PROVIDER", "openai").lower().strip()


def get_default_model():
    return os.environ.get("JVCAPTURE_LLM_MODEL") or None


def get_default_temperature():
    try:
        return float(os.environ.get("JVCAPTURE_LLM_TEMPERATURE", "0.7"))
    except (ValueError, TypeError):
        return 0.7


def get_default_max_tokens():
    try:
        return int(os.environ.get("JVCAPTURE_LLM_MAX_TOKENS", "1000"))
    except (ValueError, TypeError):
        return 1000


def get_default_api_key():
    return os.environ.get("JVCAPTURE_LLM_API_KEY") or None


def get_default_api_endpoint():
    return os.environ.get("JVCAPTURE_LLM_ENDPOINT") or None


def _resolve_api_key(provider, api_key):
    if api_key:
        return api_key
    env_key = PROVIDER_DEFAULTS[provider]["env_key"]
    if env_key:
        return os.environ.get(env_key)
    return None


def process_image(
    image_b64,
    prompt,
    provider=None,
    model=None,
    temperature=None,
    max_tokens=None,
) -> Dict[str, Any]:
    provider = (provider or get_default_provider()).lower().strip()
    if provider not in PROVIDER_DEFAULTS:
        raise LLMError(
            f"Unsupported provider: '{provider}'. "
            f"Supported: {', '.join(PROVIDER_DEFAULTS.keys())}"
        )

    defaults = PROVIDER_DEFAULTS[provider]
    model = model or get_default_model() or defaults["model"]
    temperature = temperature if temperature is not None else get_default_temperature()
    max_tokens = max_tokens if max_tokens is not None else get_default_max_tokens()
    api_key = get_default_api_key()
    api_endpoint = get_default_api_endpoint() or defaults["endpoint"]
    resolved_key = _resolve_api_key(provider, api_key)

    logger.info(
        "LLM request: provider=%s model=%s temperature=%s max_tokens=%s endpoint=%s key=%s",
        provider,
        model,
        temperature,
        max_tokens,
        api_endpoint,
        f"{resolved_key[:8]}...{resolved_key[-4:]}" if resolved_key else "None",
    )

    dispatch = {
        "openai": _call_openai,
        "anthropic": _call_anthropic,
        "ollama": _call_ollama,
        "groq": _call_groq,
        "openrouter": _call_openrouter,
    }
    result = dispatch[provider](
        image_b64=image_b64,
        prompt=prompt,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        api_key=resolved_key,
        api_endpoint=api_endpoint,
    )
    return {
        "description": result["content"],
        "tokens_used": result.get("tokens"),
    }


def _call_openai(image_b64, prompt, model, temperature, max_tokens, api_key, api_endpoint):
    if not api_key:
        raise LLMError("OpenAI API key required. Set OPENAI_API_KEY in .env or JVCAPTURE_LLM_API_KEY.")

    data_uri = f"data:image/png;base64,{image_b64}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }
    ]

    url = f"{api_endpoint.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage")
        tokens = None
        if usage:
            tokens = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        return {"content": content, "tokens": tokens}
    except httpx.HTTPStatusError as e:
        raise LLMError(f"OpenAI API error ({e.response.status_code}): {e.response.text}")
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected OpenAI response format: {e}")


def _call_anthropic(image_b64, prompt, model, temperature, max_tokens, api_key, api_endpoint):
    if not api_key:
        raise LLMError("Anthropic API key required. Set ANTHROPIC_API_KEY in .env or JVCAPTURE_LLM_API_KEY.")

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }
    ]

    url = f"{api_endpoint.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        content_blocks = data.get("content", [])
        text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
        content = "\n".join(text_parts) if text_parts else ""
        usage = data.get("usage")
        tokens = None
        if usage:
            tokens = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            }
        return {"content": content, "tokens": tokens}
    except httpx.HTTPStatusError as e:
        raise LLMError(f"Anthropic API error ({e.response.status_code}): {e.response.text}")
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected Anthropic response format: {e}")


def _call_ollama(image_b64, prompt, model, temperature, max_tokens, api_key, api_endpoint):
    messages = [
        {
            "role": "user",
            "content": prompt,
            "images": [image_b64],
        }
    ]

    url = f"{api_endpoint.rstrip('/')}/api/chat"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=300)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("message", {}).get("content", "")
        prompt_eval = data.get("prompt_eval_count")
        eval_count = data.get("eval_count")
        tokens = None
        if prompt_eval is not None or eval_count is not None:
            tokens = {
                "prompt_tokens": prompt_eval,
                "completion_tokens": eval_count,
            }
        return {"content": content, "tokens": tokens}
    except httpx.HTTPStatusError as e:
        raise LLMError(f"Ollama API error ({e.response.status_code}): {e.response.text}")
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected Ollama response format: {e}")


def _call_groq(image_b64, prompt, model, temperature, max_tokens, api_key, api_endpoint):
    if not api_key:
        raise LLMError("Groq API key required. Set GROQ_API_KEY in .env or JVCAPTURE_LLM_API_KEY.")

    data_uri = f"data:image/png;base64,{image_b64}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }
    ]

    url = f"{api_endpoint.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage")
        tokens = None
        if usage:
            tokens = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        return {"content": content, "tokens": tokens}
    except httpx.HTTPStatusError as e:
        raise LLMError(f"Groq API error ({e.response.status_code}): {e.response.text}")
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected Groq response format: {e}")


def _call_openrouter(image_b64, prompt, model, temperature, max_tokens, api_key, api_endpoint):
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMError(
            "OpenRouter API key required. Set OPENROUTER_API_KEY or OPENAI_API_KEY in .env or JVCAPTURE_LLM_API_KEY."
        )

    data_uri = f"data:image/png;base64,{image_b64}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }
    ]

    url = f"{api_endpoint.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/jvcapture",
        "X-Title": "jvcapture",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage")
        tokens = None
        if usage:
            tokens = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        return {"content": content, "tokens": tokens}
    except httpx.HTTPStatusError as e:
        raise LLMError(f"OpenRouter API error ({e.response.status_code}): {e.response.text}")
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected OpenRouter response format: {e}")