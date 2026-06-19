import time
import tempfile
from pathlib import Path

from .capture_utils import (
    launch_browser,
    close_browser,
    human_scroll,
    capture_scroll_screenshot,
    save_screenshot_to_disk,
)


def remove_generic_overlays(page):
    try:
        page.evaluate("""() => {
            document.querySelectorAll('[role="dialog"]').forEach(el => {
                if (el.offsetHeight > 100) {
                    const closeBtn = el.querySelector('button[aria-label="Close"], button.close, [class*="close"], [class*="Close"]');
                    if (closeBtn) closeBtn.click();
                    else el.remove();
                }
            });
            const cookieSelectors = [
                '#onetrust-consent-sdk', '#onetrust-banner-sdk',
                '.cookie-banner', '.cookie-banner-wrapper',
                '#cookie-notice', '.cc-window',
                '[class*="cookie"]', '[class*="Cookie"]',
                '[id*="cookie"]', '[id*="Cookie"]',
                '#sp-cc-banner', '#sp-cc-overlay',
                '.cmp_c_1100', 'div.cmp_c_2',
            ];
            cookieSelectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    if (el.offsetHeight < 600) el.remove();
                });
            });
            document.querySelectorAll('*').forEach(el => {
                try {
                    const s = window.getComputedStyle(el);
                    if (s.position === 'fixed' && parseInt(s.zIndex) > 1000 && s.display !== 'none' && el.offsetHeight > 50) {
                        const id = (el.id || '').toLowerCase();
                        const cls = (el.className || '').toString().toLowerCase();
                        if (!id.includes('header') && !cls.includes('header') && !id.includes('nav') && !cls.includes('nav') && !id.includes('navbar') && !cls.includes('navbar')) {
                            el.remove();
                        }
                    }
                } catch(e) {}
            });
        }""")
        print("  Removed generic overlays")
    except:
        pass


def capture_default(url: str, max_scrolls: int = 3, headless: bool = True, timezone: str = None, locale: str = None, proxy: str = None, proxy_timeout: int = 20, geoip: bool = None, save_image: bool = False, **kwargs) -> dict:
    tmpdir = tempfile.mkdtemp(prefix="jvcapture_default_")
    save_dir = Path(tmpdir)
    save_dir.mkdir(parents=True, exist_ok=True)

    context, browser, page = launch_browser(
        persistent_profile=None,
        headless=headless,
        human_preset="default",
        timezone=timezone,
        locale=locale,
        proxy=proxy,
        proxy_timeout=proxy_timeout,
        geoip=geoip,
    )

    try:
        print(f"Navigating to {url}...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except:
            try:
                page.goto(url, wait_until="commit", timeout=60000)
            except:
                pass
        time.sleep(5)

        remove_generic_overlays(page)
        time.sleep(1)

        human_scroll(page, distance=400, steps=3)
        time.sleep(1)

        remove_generic_overlays(page)
        time.sleep(1)

        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.hostname or "unknown"
        domain_prefix = domain.replace(".", "_").replace("/", "_")[:40]

        fullpage_path = capture_scroll_screenshot(
            page, save_dir, prefix=f"default_{domain_prefix}", max_scrolls=max_scrolls,
        )

        image_bytes = Path(fullpage_path).read_bytes()

        image_path = None
        if save_image:
            image_path = save_screenshot_to_disk(image_bytes, "unknown", domain_prefix)

        return {
            "site": "unknown",
            "url": url,
            "image_bytes": image_bytes,
            "image_path": image_path,
        }

    except Exception as e:
        print(f"Error capturing {url}: {e}")
        import traceback
        traceback.print_exc()
        try:
            err_path = save_dir / "default_error.png"
            page.screenshot(path=str(err_path), full_page=False)
            image_bytes = Path(err_path).read_bytes()
            image_path = None
            if save_image:
                image_path = save_screenshot_to_disk(image_bytes, "unknown", "error")
            return {
                "site": "unknown",
                "url": url,
                "image_bytes": image_bytes,
                "image_path": image_path,
            }
        except:
            raise

    finally:
        close_browser(context, browser)
        import shutil
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except:
            pass