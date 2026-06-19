from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional
from urllib.parse import parse_qs, urlparse, urlsplit, parse_qsl, urlencode, urlunsplit

import httpx

logger = logging.getLogger(__name__)


def _transient_retry_delay_seconds(attempt_index: int) -> float:
    base = 1.5
    max_delay = 30.0
    raw = min(base ** attempt_index, max_delay)
    return raw + random.uniform(0, 0.5)


def _is_transient_httpx_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.LocalProtocolError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return exc.response.status_code >= 500
    return False


def _webhook_url_has_query_api_key(url: str) -> bool:
    try:
        q = parse_qs(urlparse(url).query)
        vals = q.get("api_key") or []
        return any((v or "").strip() for v in vals)
    except Exception:
        return False


def _callback_headers(url: str, secret: Optional[str]) -> dict:
    h = {"Content-Type": "application/json"}
    if not _webhook_url_has_query_api_key(url) and (secret or "").strip():
        h["X-API-Key"] = (secret or "").strip()
    return h


def _append_api_key_query(url: str, api_key: str) -> str:
    key = (api_key or "").strip()
    if not key:
        return url
    parts = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "api_key"]
    q.append(("api_key", key))
    new_query = urlencode(q)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


async def post_capture_callback(
    callback_url: str,
    callback_secret: Optional[str],
    capture_url: str,
    *,
    max_retries: int = 5,
    read_timeout: float = 120.0,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> bool:
    webhook = (callback_url or "").strip()
    if not webhook:
        return False
    if not webhook.startswith(("http://", "https://")):
        logger.warning("jvcapture: callback URL not absolute: %s", webhook[:120])
        return False

    payload = {"capture_url": capture_url}
    headers = _callback_headers(webhook, callback_secret)
    timeout = httpx.Timeout(read_timeout, connect=30.0)

    last_err: Optional[str] = None
    for i in range(max_retries):
        try:
            if semaphore is not None:
                async with semaphore:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        r = await client.post(webhook, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.post(webhook, json=payload, headers=headers)

            if r.status_code in (401, 400):
                logger.error(
                    "jvcapture callback fatal HTTP %s: %s",
                    r.status_code,
                    (r.text or "")[:500],
                )
                return False
            r.raise_for_status()

            try:
                body = r.json()
            except Exception:
                logger.warning("jvcapture callback: non-JSON body")
                return True

            if isinstance(body, dict):
                result = body.get("data", body)
                if isinstance(result, dict) and result.get("imported") is True:
                    return True
                if isinstance(result, dict) and result.get("received") is True:
                    return True
                if isinstance(result, dict) and result.get("success") is True:
                    return True

            return True

        except Exception as e:
            last_err = str(e) or f"{type(e).__name__}"
            if not _is_transient_httpx_error(e):
                logger.error("jvcapture callback non-retryable: %s", e)
                return False
            delay = _transient_retry_delay_seconds(i)
            logger.warning(
                "jvcapture callback retry %s/%s in %.1fs: %s",
                i + 1,
                max_retries,
                delay,
                last_err,
            )
            if i < max_retries - 1:
                await asyncio.sleep(delay)

    logger.error("jvcapture callback failed after %d retries: %s", max_retries, last_err)
    return False