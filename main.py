import base64
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl, Field, field_validator
from .capture_utils import is_amazon_url, ProxyError
from .capture_amazon import capture_amazon
from .capture_default import capture_default

app = FastAPI(
    title="jvcapture",
    version="1.0.0",
    description=(
        "Screenshot capture API with anti-detection stealth browsing. "
        "Send a URL and get back a full-page screenshot as base64-encoded PNG. "
        "Amazon URLs are handled with specialized stealth logic (cookie dismissal, "
        "popup handling, overlay removal). All other URLs use a generic capture handler."
    ),
)


class CaptureRequest(BaseModel):
    url: HttpUrl = Field(
        ...,
        description="The URL of the page to capture as a screenshot.",
    )
    max_scrolls: int = Field(
        default=30,
        ge=1,
        le=200,
        description=(
            "Maximum number of scroll-and-screenshot steps to take while capturing "
            "the page. Each step scrolls approximately one viewport height. "
            "Increase for very long pages, decrease for faster captures of short pages."
        ),
    )

    @field_validator("max_scrolls", mode="before")
    @classmethod
    def coerce_max_scrolls(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except (ValueError, TypeError):
                raise ValueError(f"max_scrolls must be an integer, got '{v}'")
        return v
    timezone: Optional[str] = Field(
        default=None,
        description=(
            "IANA timezone for the browser session (e.g. 'America/New_York', 'Europe/London'). "
            "Sets the browser's timezone fingerprint via Chromium flags. "
            "When combined with a proxy, use this to match the proxy's timezone."
        ),
    )
    locale: Optional[str] = Field(
        default=None,
        description=(
            "BCP 47 locale for the browser session (e.g. 'en-US', 'de-DE', 'ja-JP'). "
            "Sets the browser's language and locale fingerprint. "
            "When combined with a proxy, use this to match the proxy's locale."
        ),
    )
    proxy: Optional[str] = Field(
        default=None,
        description=(
            "Proxy URL to route browser traffic through (e.g. 'http://user:pass@proxy:8080', "
            "'socks5://user:pass@proxy:1080'). The browser will appear to originate from "
            "the proxy's IP address. Supports HTTP, HTTPS, and SOCKS5 proxies."
        ),
    )
    proxy_timeout: int = Field(
        default=20,
        ge=5,
        le=60,
        description=(
            "Timeout in seconds for proxy validation (TCP connect + CONNECT handshake). "
            "Increase for slow or distant proxies. Default 20."
        ),
    )
    geoip: Optional[bool] = Field(
        default=None,
        description=(
            "Auto-detect timezone and locale from proxy IP (default: auto-enabled when proxy is provided). "
            "Requires cloakbrowser[geoip]. Explicit timezone/locale always override geoip results. "
            "Set to false to disable, true to force enable."
        ),
    )
    save_image: bool = Field(
        default=False,
        description=(
            "When true, saves the screenshot PNG to the local 'screenshots/' directory "
            "and includes the file path in the response. When false (default), the image "
            "is only returned as base64 in the response body."
        ),
    )


class CaptureResponse(BaseModel):
    site: str = Field(
        ...,
        description='The detected site type. "amazon" for Amazon URLs, "unknown" for everything else.',
    )
    url: str = Field(
        ...,
        description="The URL that was captured.",
    )
    image: str = Field(
        ...,
        description="Base64-encoded PNG screenshot of the page.",
    )
    image_path: Optional[str] = Field(
        default=None,
        description="Local file path of the saved screenshot (only present when save_image=true).",
    )


@app.post(
    "/capture",
    response_model=CaptureResponse,
    summary="Capture a screenshot of a URL",
    description=(
        "Navigate to the given URL in a stealth browser, scroll through the page, "
        "and return a stitched full-page screenshot as base64 PNG. "
        "Amazon URLs receive specialized anti-detection treatment including cookie banner "
        "dismissal, sign-in popup closing, \"Continue Shopping\" dismissal, and overlay removal. "
        "All other URLs are captured with a generic handler that attempts basic cookie "
        "and overlay dismissal.\n\n"
        "The `site` field in the response indicates which handler was used: "
        "\"amazon\" for Amazon URLs, \"unknown\" for all others.\n\n"
        "Location/spoofing options:\n"
        "- **timezone**: Set the browser's IANA timezone (e.g. `America/New_York`)\n"
        "- **locale**: Set the browser's BCP 47 locale (e.g. `en-US`)\n"
        "- **proxy**: Route traffic through a proxy (e.g. `http://user:pass@proxy:8080`)\n\n"
        "Combine proxy + timezone + locale to make the browser appear to be in a specific location.\n\n"
        "Set **save_image=true** to also save the screenshot PNG to the local `screenshots/` directory."
    ),
)
def capture(req: CaptureRequest):
    url = str(req.url)
    kwargs = dict(
        url=url,
        max_scrolls=req.max_scrolls,
        headless=True,
        timezone=req.timezone,
        locale=req.locale,
        proxy=req.proxy,
        proxy_timeout=req.proxy_timeout,
        geoip=req.geoip,
        save_image=req.save_image,
    )

    try:
        if is_amazon_url(url):
            result = capture_amazon(**kwargs)
        else:
            result = capture_default(**kwargs)
    except ProxyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Capture failed: {str(e)}")

    image_b64 = base64.b64encode(result["image_bytes"]).decode("utf-8")

    return CaptureResponse(
        site=result["site"],
        url=result["url"],
        image=image_b64,
        image_path=result.get("image_path"),
    )


@app.get(
    "/health",
    summary="Health check",
    description="Returns OK if the service is running and ready to accept requests.",
)
def health():
    return {"status": "ok"}