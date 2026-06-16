import logging
logging.disable(logging.ERROR)
import hashlib
logging.disable(logging.NOTSET)

import socket
import struct
import time
import json
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from cloakbrowser import launch, launch_persistent_context

FINGERPRINT_SEED = "84721"


class ProxyError(Exception):
    pass


def _tcp_connect(host: str, port: int, timeout: float) -> socket.socket:
    """TCP connect to host:port, return the connected socket. Raises ProxyError on failure."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return sock
    except (socket.timeout, OSError) as e:
        try:
            sock.close()
        except Exception:
            pass
        raise ProxyError(f"Proxy unreachable: cannot connect to {host}:{port} ({e.__class__.__name__}: {e})")


def _probe_http_connect(sock: socket.socket, host: str, port: int, timeout: float) -> str:
    """Try HTTP CONNECT tunnel. Returns 'http' on success, None if no response, or raises ProxyError."""
    connect_request = f"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
    try:
        sock.sendall(connect_request.encode())
        sock.settimeout(timeout)
        response = sock.recv(4096).decode(errors="replace")

        if not response:
            return None

        status_line = response.split("\r\n")[0]
        parts = status_line.split(" ", 2)
        status_code = parts[1] if len(parts) >= 2 else ""
        reason = parts[2] if len(parts) > 2 else ""

        if status_code == "200":
            print(f"  HTTP proxy validated: {host}:{port} (CONNECT OK)")
            return "http"
        elif status_code == "403":
            raise ProxyError(f"Proxy {host}:{port} rejected CONNECT: 403 {reason}. The proxy blocks HTTPS traffic.")
        elif status_code == "407":
            raise ProxyError(f"Proxy {host}:{port} requires authentication: 407 {reason}. Provide credentials in the proxy URL (e.g. http://user:pass@host:port).")
        else:
            raise ProxyError(f"Proxy {host}:{port} returned unexpected CONNECT response: {status_line}. It may not support HTTPS tunneling.")
    except ProxyError:
        raise
    except (socket.timeout, OSError) as e:
        raise ProxyError(f"HTTP CONNECT test failed for {host}:{port} ({e.__class__.__name__}: {e})")


def _probe_socks5(sock: socket.socket, host: str, port: int, timeout: float, username: str = None, password: str = None) -> bool:
    """Try SOCKS5 handshake and CONNECT. Returns True on success or raises ProxyError."""
    try:
        if username:
            auth_methods = b"\x05\x02\x00\x02"
        else:
            auth_methods = b"\x05\x01\x00"
        sock.sendall(auth_methods)
        sock.settimeout(timeout)
        resp = sock.recv(2)
        if len(resp) < 2 or resp[0] != 5:
            return False

        if resp[1] == 2 and username:
            if not password:
                password = ""
            user_bytes = username.encode()
            pass_bytes = password.encode()
            auth_msg = b"\x01" + bytes([len(user_bytes)]) + user_bytes + bytes([len(pass_bytes)]) + pass_bytes
            sock.sendall(auth_msg)
            auth_resp = sock.recv(2)
            if len(auth_resp) < 2 or auth_resp[1] != 0:
                raise ProxyError(f"SOCKS5 proxy {host}:{port}: authentication failed (bad username/password).")
        elif resp[1] == 0xFF:
            raise ProxyError(f"SOCKS5 proxy {host}:{port}: no acceptable auth method (requires credentials).")
        elif resp[1] not in (0, 2):
            return False

        connect_msg = (
            b"\x05\x01\x00\x03"
            + bytes([len(b"example.com")]) + b"example.com"
            + struct.pack("!H", 443)
        )
        sock.sendall(connect_msg)
        connect_resp = sock.recv(10)
        if len(connect_resp) < 4 or connect_resp[0] != 5:
            return False

        rep = connect_resp[1]
        socks5_errors = {
            1: "general SOCKS server failure",
            2: "connection not allowed by ruleset",
            3: "network unreachable",
            4: "host unreachable",
            5: "connection refused by target",
            6: "TTL expired",
            7: "command not supported",
            8: "address type not supported",
        }
        if rep == 0:
            print(f"  SOCKS5 proxy validated: {host}:{port} (CONNECT to example.com:443 OK)")
            return True
        else:
            desc = socks5_errors.get(rep, f"unknown error code {rep}")
            raise ProxyError(f"SOCKS5 proxy {host}:{port}: {desc}.")
    except ProxyError:
        raise
    except (socket.timeout, OSError) as e:
        raise ProxyError(f"SOCKS5 handshake with {host}:{port} failed ({e.__class__.__name__}: {e})")


def _probe_socks4(sock: socket.socket, host: str, port: int, timeout: float) -> bool:
    """Try SOCKS4 CONNECT. Returns True on success or raises ProxyError."""
    try:
        connect_msg = b"\x04\x01\x01\xbb" + b"\x00\x00\x00\x01" + b"\x00"
        sock.sendall(connect_msg)
        sock.settimeout(timeout)
        resp = sock.recv(8)
        if len(resp) < 2 or resp[0] != 0:
            return False
        code = resp[1]
        if code == 90:
            print(f"  SOCKS4 proxy validated: {host}:{port} (CONNECT OK)")
            return True
        socks4_errors = {
            91: "request rejected or failed",
            92: "cannot connect to identd",
            93: "identd reports different user",
        }
        desc = socks4_errors.get(code, f"unknown response code {code}")
        raise ProxyError(f"SOCKS4 proxy {host}:{port}: {desc}.")
    except ProxyError:
        raise
    except (socket.timeout, OSError) as e:
        raise ProxyError(f"SOCKS4 handshake with {host}:{port} failed ({e.__class__.__name__}: {e})")


def validate_proxy(proxy: str, timeout: int = 20) -> str:
    """Normalize, auto-detect, and validate a proxy string.

    Supports HTTP(S), SOCKS4, SOCKS5, and SOCKS5h proxies.
    Auto-detects proxy type when no scheme is provided.
    Returns the normalized proxy URL string.
    Raises ProxyError if the proxy is unreachable or cannot tunnel HTTPS.
    """
    if not proxy:
        return proxy

    parsed_original = urlparse(proxy) if "://" in proxy else None
    if parsed_original and parsed_original.scheme.lower() in ("http", "https", "socks4", "socks5", "socks5h"):
        scheme = parsed_original.scheme.lower()
        host = parsed_original.hostname
        port = parsed_original.port
        username = parsed_original.username
        password = parsed_original.password
        if not host or not port:
            raise ProxyError(f"Invalid proxy format: '{proxy}'. Expected 'scheme://host:port'.")
    else:
        scheme = None
        bare = proxy
        if "@" in bare:
            userinfo, bare = bare.rsplit("@", 1)
            if ":" in userinfo:
                username, password = userinfo.split(":", 1)
            else:
                username, password = userinfo, None
        else:
            username, password = None, None
        host_port = bare.rsplit(":", 1)
        if len(host_port) != 2:
            raise ProxyError(f"Invalid proxy format: '{proxy}'. Expected 'host:port' or 'scheme://host:port'.")
        host = host_port[0]
        try:
            port = int(host_port[1])
        except ValueError:
            raise ProxyError(f"Invalid proxy port: '{host_port[1]}'. Must be a number.")

    print(f"  Validating proxy: {host}:{port} ...")

    # Step 1: TCP connectivity check
    try:
        sock = _tcp_connect(host, port, timeout)
        sock.close()
    except ProxyError:
        raise

    # Step 2: Protocol probe — if scheme known, probe only that; if unknown, try all
    if scheme in ("socks5", "socks5h"):
        sock = _tcp_connect(host, port, timeout)
        try:
            _probe_socks5(sock, host, port, timeout, username, password)
            effective_scheme = scheme
        finally:
            sock.close()
    elif scheme == "socks4":
        sock = _tcp_connect(host, port, timeout)
        try:
            _probe_socks4(sock, host, port, timeout)
            effective_scheme = "socks4"
        finally:
            sock.close()
    elif scheme in ("http", "https"):
        sock = _tcp_connect(host, port, timeout)
        try:
            result = _probe_http_connect(sock, host, port, timeout)
            if result:
                effective_scheme = result
            else:
                raise ProxyError(f"HTTP proxy {host}:{port} accepted connection but closed it without responding. It may be a SOCKS proxy — try socks5://{host}:{port} or socks4://{host}:{port}.")
        finally:
            sock.close()
    else:
        # No scheme provided — auto-detect by probing HTTP first, then SOCKS5, then SOCKS4
        print(f"  No scheme provided — auto-detecting proxy type...")
        detected_scheme = None
        probe_errors = []

        probes = [
            ("http", lambda: _probe_http_connect(_tcp_connect(host, port, timeout), host, port, timeout)),
            ("socks5", lambda: _probe_socks5(_tcp_connect(host, port, timeout), host, port, timeout, username, password)),
            ("socks4", lambda: _probe_socks4(_tcp_connect(host, port, timeout), host, port, timeout)),
        ]

        for probe_scheme, probe_fn in probes:
            try:
                result = probe_fn()
                if result:
                    detected_scheme = probe_scheme
                    break
                else:
                    probe_errors.append(f"{probe_scheme}: no response (connection closed)")
            except ProxyError as e:
                probe_errors.append(f"{probe_scheme}: {e}")
            except Exception as e:
                probe_errors.append(f"{probe_scheme}: unexpected error ({e.__class__.__name__}: {e})")

        if detected_scheme:
            effective_scheme = detected_scheme
            print(f"  Auto-detected proxy type: {effective_scheme}")
        else:
            raise ProxyError(
                f"Could not auto-detect proxy type for {host}:{port}. "
                f"Probe results:\n" +
                "\n".join(f"    {err}" for err in probe_errors) +
                "\n  Try specifying the scheme explicitly (e.g. socks5://host:port or http://host:port)."
            )

    userinfo = ""
    if username:
        userinfo = f"{username}:{password}@" if password else f"{username}@"
    normalized = f"{effective_scheme}://{userinfo}{host}:{port}"
    print(f"  Proxy URL: {normalized}")
    return normalized


def launch_browser(persistent_profile=None, headless=False, human_preset="careful", extra_args=None, timezone=None, locale=None, proxy=None, proxy_timeout=20, geoip=None):
    args = [f"--fingerprint={FINGERPRINT_SEED}"]
    if extra_args:
        args.extend(extra_args)

    if proxy:
        proxy = validate_proxy(proxy, timeout=proxy_timeout)

    effective_geoip = geoip if geoip is not None else bool(proxy)

    launch_kwargs = dict(headless=headless, humanize=True, human_preset=human_preset, args=args)
    if timezone:
        launch_kwargs["timezone"] = timezone
    if locale:
        launch_kwargs["locale"] = locale
    if proxy:
        launch_kwargs["proxy"] = proxy
    if effective_geoip:
        launch_kwargs["geoip"] = True

    if persistent_profile:
        print(f"  Using persistent profile: {persistent_profile}")
        if timezone:
            print(f"  Timezone: {timezone}")
        if locale:
            print(f"  Locale: {locale}")
        if proxy:
            print(f"  Proxy: {proxy}")
        if effective_geoip:
            print(f"  GeoIP: auto-detecting timezone/locale from proxy IP")
        context = launch_persistent_context(persistent_profile, **launch_kwargs)
        page = context.new_page()
        return context, None, page
    else:
        if timezone:
            print(f"  Timezone: {timezone}")
        if locale:
            print(f"  Locale: {locale}")
        if proxy:
            print(f"  Proxy: {proxy}")
        if effective_geoip:
            print(f"  GeoIP: auto-detecting timezone/locale from proxy IP")
        browser = launch(**launch_kwargs)
        page = browser.new_page()
        return None, browser, page


def close_browser(context, browser):
    if context:
        context.close()
    if browser:
        browser.close()


def human_scroll(page, distance=800, steps=8):
    time.sleep(0.3)
    for i in range(steps):
        step = distance // steps
        page.mouse.wheel(0, step)
        pause = 0.1 + (hash(str(i)) % 30) / 100.0
        time.sleep(pause + 0.05)
    time.sleep(0.5)


def human_browse(page, duration=8):
    human_scroll(page, distance=600, steps=5)
    time.sleep(2)
    human_scroll(page, distance=400, steps=4)
    remaining = max(1, duration - 5)
    time.sleep(remaining)


def human_scroll_to_top(page):
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1)


def capture_scroll_screenshot(page, save_dir, prefix="scroll", overlap=60, delay=0.4, max_scrolls=30):
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.5)

    viewport_height = page.evaluate("window.innerHeight")
    document_height = page.evaluate(
        "document.body ? Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) : document.documentElement.scrollHeight"
    )

    step = viewport_height - overlap
    total_steps = max(1, int((document_height - viewport_height) / step) + 1)
    num_chunks = min(total_steps + 1, max_scrolls)

    print(f"  Viewport: {viewport_height}px, Document: {document_height}px, "
          f"Step: {step}px, Scrolls: {num_chunks}")

    chunks_dir = save_dir / f"{prefix}_chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    chunk_paths = []
    seen_positions = set()

    for i in range(num_chunks):
        target_y = i * step
        if target_y > document_height - viewport_height:
            target_y = max(0, document_height - viewport_height)

        page.evaluate(f"window.scrollTo(0, {target_y})")
        time.sleep(delay)

        actual_y = page.evaluate(
            "Math.round(window.pageYOffset || document.documentElement.scrollTop)"
        )

        if actual_y in seen_positions and i > 0:
            prev_top = page.evaluate(f"Math.round(window.pageYOffset) - {overlap}")
            page.evaluate(f"window.scrollTo(0, {prev_top})")
            time.sleep(delay / 2)
            actual_y = page.evaluate(
                "Math.round(window.pageYOffset || document.documentElement.scrollTop)"
            )

        if actual_y in seen_positions:
            print(f"  Skipping duplicate chunk at scroll={actual_y}")
            continue

        seen_positions.add(actual_y)

        chunk_path = chunks_dir / f"{prefix}_{i:03d}.png"
        page.screenshot(path=str(chunk_path), full_page=False)
        chunk_paths.append((str(chunk_path), actual_y))
        print(f"  Screenshot chunk {i}/{num_chunks}: target={target_y}, actual={actual_y}")

        max_scroll = document_height - viewport_height
        if actual_y >= max_scroll - 5:
            print(f"  Reached bottom of page at scroll={actual_y}")
            break

    if not chunk_paths:
        chunk_path = chunks_dir / f"{prefix}_000.png"
        page.screenshot(path=str(chunk_path), full_page=False)
        chunk_paths.append((str(chunk_path), 0))

    stitched_path = save_dir / f"{prefix}_fullpage.png"
    try:
        from PIL import Image
        images = []
        for p, _ in chunk_paths:
            images.append(Image.open(p))

        if len(images) == 1:
            images[0].save(str(stitched_path))
            images[0].close()
            print(f"  Single-chunk screenshot: {stitched_path}")
        else:
            total_width = max(img.width for img in images)
            total_height = images[0].height + sum(
                img.height - overlap for img in images[1:]
            )
            stitched = Image.new("RGB", (total_width, total_height))
            y_offset = 0
            for i, img in enumerate(images):
                crop_top = overlap if i > 0 else 0
                cropped = img.crop((0, crop_top, img.width, img.height))
                stitched.paste(cropped, (0, y_offset))
                y_offset += cropped.height
                img.close()
            stitched.save(str(stitched_path))
            stitched.close()
            print(f"  Stitched full-page screenshot: {stitched_path}")

        for p, _ in chunk_paths:
            try:
                Path(p).unlink()
            except Exception:
                pass
        try:
            chunks_dir.rmdir()
        except Exception:
            pass
    except ImportError:
        print("  Pillow not installed, keeping individual chunk screenshots")
        stitched_path = chunk_paths[-1][0] if chunk_paths else None

    return str(stitched_path)


SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"


def save_screenshot_to_disk(image_bytes: bytes, site: str, item_id: str) -> str:
    from datetime import datetime
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{site}_{item_id}_{timestamp}.png"
    filepath = SCREENSHOTS_DIR / filename
    filepath.write_bytes(image_bytes)
    print(f"  Saved screenshot to: {filepath}")
    return str(filepath)


def extract_domain(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or ""


def is_amazon_url(url: str) -> bool:
    domain = extract_domain(url).lower()
    return "amazon.com" in domain or "amazon." in domain