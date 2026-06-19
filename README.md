# jvcapture

Screenshot capture API with anti-detection stealth browsing. Send a URL, get back a full-page screenshot with site classification.

## Features

- **Site detection** — Automatically classifies URLs as built-in (`amazon`) or unknown sites
- **Amazon stealth capture** — Amazon URLs get specialized anti-detection browsing: persistent profiles, cookie banner dismissal, "Continue Shopping" popup dismissal, overlay removal, and human-like scrolling
- **Generic capture** — Unknown sites get a clean screenshot with automatic cookie/overlay dismissal
- **Location spoofing** — Set `timezone`, `locale`, and `proxy` to make the browser appear from a specific location
- **Scroll control** — `max_scrolls` parameter controls how many scroll-and-screenshot steps to take (default 30, one step ≈ one viewport height)
- **Local save** — `save_image=true` saves the screenshot PNG to `jvcapture/screenshots/` and returns the file path
- **Base64 response** — Image always comes back as base64-encoded PNG in the JSON response
- **Health endpoint** — `GET /health` for uptime monitoring

## Quick Start

### Install dependencies

```bash
cd /Users/tharickjairam/jvsproject
source .venv/bin/activate
pip install -r jvcapture/requirements.txt
```

### Run the server

```bash
cd /Users/tharickjairam/jvsproject
.venv/bin/uvicorn jvcapture.main:app --host 0.0.0.0 --port 8001
```

For development with auto-reload:

```bash
.venv/bin/uvicorn jvcapture.main:app --reload --port 8001
```

### API Docs

Once running, visit:

- Swagger UI: `http://localhost:8001/docs`
- ReDoc: `http://localhost:8001/redoc`

## API Reference

### `POST /capture`

Navigate to a URL in a stealth browser, scroll through the page, and return a stitched full-page screenshot as base64 PNG. Amazon URLs receive specialized anti-detection treatment (cookie banner dismissal, sign-in popup closing, "Continue Shopping" popup dismissal, overlay removal). All other URLs are captured with a generic handler that attempts basic cookie and overlay dismissal.

**Request body:**

| Field         | Type         | Required | Default | Description                                                                                                           |
| ------------- | ------------ | -------- | ------- | --------------------------------------------------------------------------------------------------------------------- |
| `url`         | string (URL) | Yes      | —       | The URL of the page to capture                                                                                        |
| `max_scrolls` | integer      | No       | `3`     | Maximum number of scroll-and-screenshot steps. Each step scrolls approximately one viewport height. Range: 1–200.     |
| `timezone`    | string       | No       | `null`  | IANA timezone for the browser session (e.g. `America/New_York`). Sets the browser's timezone fingerprint.             |
| `locale`      | string       | No       | `null`  | BCP 47 locale for the browser session (e.g. `en-US`). Sets the browser's language/locale fingerprint.                 |
| `proxy`       | string       | No       | `null`  | Proxy URL to route traffic through (e.g. `http://user:pass@proxy:8080`, `socks5://user:pass@proxy:1080`).             |
| `save_image`  | boolean      | No       | `false` | When true, saves the screenshot PNG to the local `screenshots/` directory and includes the file path in the response. |

**Response fields:**

| Field        | Type         | Description                                                                                     |
| ------------ | ------------ | ----------------------------------------------------------------------------------------------- |
| `site`       | string       | `"amazon"` for Amazon URLs, `"unknown"` for everything else                                     |
| `url`        | string       | The URL that was captured                                                                       |
| `image`      | string       | Base64-encoded PNG screenshot of the page                                                       |
| `image_path` | string\|null | Local file path of the saved screenshot (only present when `save_image=true`, `null` otherwise) |

**Example — basic capture:**

```bash
curl -X POST http://localhost:8001/capture \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.amazon.com/dp/B0FQC8QJC4", "max_scrolls": 20}'
```

**Example — with location spoofing:**

```bash
curl -X POST http://localhost:8001/capture \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.amazon.com/dp/B0FQC8QJC4",
    "max_scrolls": 30,
    "timezone": "America/New_York",
    "locale": "en-US",
    "proxy": "http://user:pass@proxy.example.com:8080"
  }'
```

**Example — save image locally:**

```bash
curl -X POST http://localhost:8001/capture \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.amazon.com/dp/B0FQC8QJC4", "save_image": true}'
```

Response when `save_image=true`:

```json
{
  "site": "amazon",
  "url": "https://www.amazon.com/dp/B0FQC8QJC4",
  "image": "iVBORw0KGgoAAAANSUhEUg...",
  "image_path": "/Users/tharickjairam/jvsproject/jvcapture/screenshots/amazon_B0FQC8QJC4_20260615_143022.png"
}
```

Response when `save_image=false` (default):

```json
{
  "site": "amazon",
  "url": "https://www.amazon.com/dp/B0FQC8QJC4",
  "image": "iVBORw0KGgoAAAANSUhEUg...",
  "image_path": null
}
```

**Site values:**

| Site      | Condition                                       |
| --------- | ----------------------------------------------- |
| `amazon`  | URL hostname contains `amazon.com` or `amazon.` |
| `unknown` | All other URLs                                  |

### Location / Geo-spoofing

Cloakbrowser supports location-aware browsing through three parameters:

| Parameter  | What it does                                                         | Example values                      |
| ---------- | -------------------------------------------------------------------- | ----------------------------------- |
| `timezone` | Sets the browser's IANA timezone via Chromium fingerprint flags      | `America/New_York`, `Europe/London` |
| `locale`   | Sets the browser's BCP 47 locale (language + region)                 | `en-US`, `de-DE`, `ja-JP`           |
| `proxy`    | Routes all traffic through a proxy, changing the apparent IP address | `http://user:pass@proxy:8080`       |

**Common combinations:**

| Location      | timezone           | locale  | proxy (example)                       |
| ------------- | ------------------ | ------- | ------------------------------------- |
| US East Coast | `America/New_York` | `en-US` | `http://user:pass@us-east-proxy:8080` |
| UK            | `Europe/London`    | `en-GB` | `http://user:pass@uk-proxy:8080`      |
| Germany       | `Europe/Berlin`    | `de-DE` | `http://user:pass@de-proxy:8080`      |
| Japan         | `Asia/Tokyo`       | `ja-JP` | `http://user:pass@jp-proxy:8080`      |

Proxy supports HTTP, HTTPS, and SOCKS5:

- HTTP: `http://user:pass@proxy:8080`
- HTTPS: `https://user:pass@proxy:8080`
- SOCKS5: `socks5://user:pass@proxy:1080`

### `GET /health`

Returns OK if the service is running and ready to accept requests.

```bash
curl http://localhost:8001/health
```

```json
{ "status": "ok" }
```

## Saving the Image

The `image` field is always returned as base64-encoded PNG. To save it as a file:

```python
import base64, json

resp = json.loads(response_text)
with open("screenshot.png", "wb") as f:
    f.write(base64.b64decode(resp["image"]))
```

Or from the command line:

```bash
curl -s -X POST http://localhost:8001/capture \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.amazon.com/dp/B0FQC8QJC4"}' \
  | python3 -c "import sys,json,base64; d=json.load(sys.stdin); open('out.png','wb').write(base64.b64decode(d['image']))"
```

When `save_image=true`, the image is also saved to `jvcapture/screenshots/` automatically and the `image_path` field contains the local file path.

## Project Structure

```
jvcapture/
  main.py              # FastAPI app, POST /capture, GET /health
  capture_amazon.py    # Amazon-specific stealth handler
  capture_default.py   # Generic site handler
  capture_utils.py     # Shared browser launch, scroll, screenshot utilities
  requirements.txt     # Python dependencies
  screenshots/         # Local screenshot storage (when save_image=true)
```

## Extending with New Sites

To add a new built-in site (e.g. eBay):

1. Create `capture_ebay.py` with a `capture_ebay(url, max_scrolls, headless, timezone, locale, proxy, save_image)` function returning `{"site": "ebay", "url": url, "image_bytes": bytes, "image_path": str|None}`
2. Add a detection function in `capture_utils.py` (e.g. `is_ebay_url()`)
3. Wire it up in `main.py`
