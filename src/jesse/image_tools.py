"""Image analysis tools for Jesse — vision-capable LLM chart analysis."""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

MAX_IMAGE_SIZE = 10 * 1024 * 1024
FETCH_TIMEOUT = 15.0
ALLOWED_MEDIA_DIR = Path.home() / ".mt5-mcp" / "media"

MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def _resolve_vision_llm(llm=None):
    """Resolve vision-capable LLM from env vars or use provided LLM."""
    if llm is not None:
        return llm
    model = os.environ.get(
        "JESSE_VISION_MODEL", os.environ.get("JESSE_MODEL", "coder-model")
    )
    base_url = os.environ.get(
        "JESSE_VISION_BASE_URL",
        os.environ.get("JESSE_BASE_URL", "http://127.0.0.1:3000/v1"),
    )
    api_key = os.environ.get(
        "JESSE_VISION_API_KEY", os.environ.get("JESSE_API_KEY", "not-needed")
    )
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        openai_api_base=base_url,
        openai_api_key=api_key,
        temperature=0.1,
        max_tokens=2048,
    )


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def _validate_url(url: str) -> None:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    private_patterns = [
        "127.",
        "10.",
        "192.168.",
        "172.16.",
        "172.17.",
        "172.18.",
        "172.19.",
        "172.20.",
        "172.21.",
        "172.22.",
        "172.23.",
        "172.24.",
        "172.25.",
        "172.26.",
        "172.27.",
        "172.28.",
        "172.29.",
        "172.30.",
        "172.31.",
        "169.254.",
        "0.",
    ]
    if (
        hostname == "localhost"
        or hostname == "::1"
        or any(hostname.startswith(p) for p in private_patterns)
    ):
        raise ValueError(f"Access to private/reserved addresses is blocked: {hostname}")
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Invalid URL scheme: {parsed.scheme}. Only http:// and https:// are allowed."
        )


async def _fetch_image_as_base64(source: str) -> tuple[str, str]:
    if _is_url(source):
        _validate_url(source)
        import httpx

        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            resp = await client.get(source)
            resp.raise_for_status()
            if len(resp.content) > MAX_IMAGE_SIZE:
                raise ValueError(
                    f"Image exceeds {MAX_IMAGE_SIZE / (1024 * 1024):.0f}MB limit"
                )
            mime = resp.headers.get("content-type", "image/jpeg")
            return base64.b64encode(resp.content).decode(), mime
    else:
        resolved = Path(source).resolve()
        allowed = [ALLOWED_MEDIA_DIR.resolve(), Path.cwd().resolve()]
        if not any(str(resolved).startswith(str(a)) for a in allowed):
            raise ValueError(f"Path traversal blocked: {source}")
        ext = resolved.suffix.lower()
        if ext not in MIME_MAP:
            raise ValueError(f"Invalid image extension: {ext}")
        if resolved.stat().st_size > MAX_IMAGE_SIZE:
            raise ValueError(
                f"Image exceeds {MAX_IMAGE_SIZE / (1024 * 1024):.0f}MB limit"
            )
        data = resolved.read_bytes()
        return base64.b64encode(data).decode(), MIME_MAP.get(ext, "image/jpeg")


async def _send_to_vision_llm(
    base64_data: str, mime_type: str, prompt: str, llm=None
) -> str:
    llm = _resolve_vision_llm(llm)
    response = await llm.ainvoke(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_data}"},
                    },
                ],
            }
        ]
    )
    return response.content


def _is_base64(s: str) -> bool:
    if len(s) < 10:
        return False
    try:
        if not re.match(r"^[A-Za-z0-9+/=]+$", s.strip()):
            return False
        base64.b64decode(s[:100], validate=True)
        return True
    except Exception:
        return False


def make_image_tools(mcp_client: Any = None, llm: Any = None) -> list:
    """Create LangChain tools for image/chart analysis."""

    @tool
    async def image_analyze(
        image_source: str,
        prompt: str = "Describe this chart's market structure, support/resistance levels, and any notable patterns.",
    ) -> str:
        """Analyze an image using AI vision. Accepts file path, URL, or base64 string. Returns text description."""
        try:
            if image_source.startswith("data:image") or _is_base64(image_source):
                if image_source.startswith("data:image"):
                    header, b64_data = image_source.split(",", 1)
                    mime_type = header.split(":")[1].split(";")[0]
                else:
                    mime_type = "image/png"
                    b64_data = image_source
                return await _send_to_vision_llm(b64_data, mime_type, prompt, llm)

            b64_data, mime_type = await _fetch_image_as_base64(image_source)
            return await _send_to_vision_llm(b64_data, mime_type, prompt, llm)

        except ValueError as exc:
            return f"Image analysis failed: {exc}"
        except Exception as exc:
            logger.exception("Unexpected error in image_analyze")
            return f"Image analysis failed: {type(exc).__name__}: {exc}"

    @tool
    async def analyze_chart(
        symbol: str,
        timeframe: str = "H1",
        prompt: str = "Describe this chart's market structure, support/resistance levels, trend direction, and any notable candlestick patterns.",
    ) -> str:
        """Analyze a chart screenshot for the given symbol/timeframe. Gets screenshot from MT5 and analyzes it with vision."""
        if mcp_client is None:
            return "Error: mcp_client not initialized. Ensure the agent is properly configured."

        try:
            result = await mcp_client.get_chart_screenshot(symbol, timeframe)

            if isinstance(result, dict):
                base64_data = (
                    result.get("screenshot")
                    or result.get("data")
                    or result.get("image")
                )
                if not base64_data:
                    base64_data = result.get("content", result.get("base64", ""))
            elif isinstance(result, str):
                base64_data = result
            else:
                return f"Unexpected response format from get_chart_screenshot: {type(result).__name__}"

            if not base64_data:
                return f"No screenshot data returned for {symbol} {timeframe}. Response: {result}"

            return await _send_to_vision_llm(base64_data, "image/png", prompt, llm)

        except ValueError as exc:
            return f"Chart analysis failed: {exc}"
        except Exception as exc:
            logger.exception("Unexpected error in analyze_chart")
            return f"Chart analysis failed: {type(exc).__name__}: {exc}"

    return [image_analyze, analyze_chart]
