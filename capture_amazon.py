import logging
logging.disable(logging.ERROR)
import hashlib
logging.disable(logging.NOTSET)

import time
import re
import tempfile
from pathlib import Path

from .capture_utils import (
    launch_browser,
    close_browser,
    human_scroll,
    human_browse,
    capture_scroll_screenshot,
    save_screenshot_to_disk,
    ProxyError,
)


def dismiss_amazon_cookie_banner(page, timeout=8000):
    for selector, label in [
        ("#sp-cc-accept", "sp-cc-accept"),
        ("#a-autoid-0", "a-autoid-0"),
    ]:
        try:
            btn = page.locator(selector)
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                print(f"  Accepted Amazon cookies ({label})")
                time.sleep(1.5)
                return True
        except:
            pass
    try:
        page.wait_for_selector("#sp-cc-accept", state="visible", timeout=timeout)
        page.click("#sp-cc-accept")
        print("  Accepted Amazon cookies (sp-cc-accept wait)")
        time.sleep(1.5)
        return True
    except:
        pass
    try:
        accept = page.locator("text=Accept Cookies, text=Continue, text=Accept All")
        if accept.count() > 0:
            accept.first.click()
            print("  Accepted Amazon cookies (text)")
            time.sleep(1.5)
            return True
    except:
        pass
    return False


def close_amazon_signin_popup(page):
    try:
        tooltip = page.locator("#nav-signin-tooltip, .nav-signin-t.nav-line-2")
        if tooltip.count() > 0 and tooltip.first.is_visible():
            close_btn = tooltip.first.locator("[aria-label='Close'], .nav-signin-close, button.close")
            if close_btn.count() > 0:
                close_btn.first.click()
                print("  Closed Amazon sign-in tooltip")
                time.sleep(0.5)
                return True
    except:
        pass
    try:
        dialog = page.locator("[role='dialog']")
        if dialog.count() > 0 and dialog.first.is_visible():
            close_btn = dialog.first.locator("button[aria-label='Close'], button.a-button-close, [class*='close']")
            if close_btn.count() > 0:
                close_btn.first.click()
                print("  Closed Amazon dialog")
                time.sleep(0.5)
                return True
            dialog.first.evaluate("el => el.remove()")
            print("  Removed Amazon dialog via JS")
            time.sleep(0.5)
            return True
    except:
        pass
    return False


def remove_amazon_overlays(page):
    try:
        page.evaluate("""() => {
            document.querySelectorAll('#sp-cc-banner, #sp-cc-overlay, #nav-flyout-ewc, .nav-flyout-buffer').forEach(el => el.remove());
            document.querySelectorAll('[role="dialog"]').forEach(el => {
                if (el.offsetHeight > 100 && !el.id.includes('nav-cart')) {
                    const closeBtn = el.querySelector('button[aria-label="Close"], button.a-button-close, [class*="close"]');
                    if (closeBtn) closeBtn.click();
                    else el.remove();
                }
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
        print("  Removed Amazon overlays")
    except:
        pass


def dismiss_amazon_continue_shopping(page, timeout=5000):
    for selector in [
        "input[name='wlContinueShopping']",
        "#wlContinueShopping",
        "#a-autoid-1",
        "input.a-button-input[value='Continue Shopping']",
        "button.a-button-text:has-text('Continue shopping')",
    ]:
        try:
            btn = page.locator(selector)
            if btn.count() > 0 and btn.is_visible():
                btn.first.click()
                print(f"  Clicked Continue Shopping ({selector})")
                time.sleep(1.5)
                return True
        except:
            pass
    try:
        continue_btn = page.locator("text=Continue shopping")
        if continue_btn.count() > 0:
            continue_btn.first.click()
            print("  Clicked Continue Shopping (text)")
            time.sleep(1.5)
            return True
    except:
        pass
    try:
        dialog = page.locator("[role='dialog']")
        if dialog.count() > 0 and dialog.first.is_visible():
            continue_btn = dialog.first.locator("text=Continue shopping, text=Continue Shopping")
            if continue_btn.count() > 0:
                continue_btn.first.click()
                print("  Clicked Continue Shopping in dialog")
                time.sleep(1.5)
                return True
    except:
        pass
    try:
        page.evaluate("""() => {
            document.querySelectorAll('input[name="wlContinueShopping"], #wlContinueShopping').forEach(el => el.click());
            document.querySelectorAll('button, input[type="button"], input[type="submit"]').forEach(el => {
                const t = (el.value || el.textContent || '').toLowerCase();
                if (t.includes('continue shopping') || t === 'continue') el.click();
            });
        }""")
        print("  Attempted Continue Shopping via JS")
        time.sleep(1)
    except:
        pass
    return False


def dismiss_all_amazon_popups(page, cookie_timeout=8000):
    dismiss_amazon_cookie_banner(page, timeout=cookie_timeout)
    close_amazon_signin_popup(page)
    dismiss_amazon_continue_shopping(page)
    time.sleep(1)
    dismiss_amazon_cookie_banner(page, timeout=3000)
    close_amazon_signin_popup(page)
    dismiss_amazon_continue_shopping(page)


def capture_amazon(url: str, max_scrolls: int = 30, headless: bool = True, timezone: str = None, locale: str = None, proxy: str = None, proxy_timeout: int = 20, geoip: bool = None, save_image: bool = False) -> dict:
    tmpdir = tempfile.mkdtemp(prefix="jvcapture_amazon_")
    save_dir = Path(tmpdir)
    save_dir.mkdir(parents=True, exist_ok=True)

    profile = str(save_dir / ".amazon_profile")
    context, browser, page = launch_browser(
        persistent_profile=profile,
        headless=headless,
        human_preset="careful",
        timezone=timezone,
        locale=locale,
        proxy=proxy,
        proxy_timeout=proxy_timeout,
        geoip=geoip,
    )

    try:
        print("Navigating directly to Amazon product page...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except:
            try:
                page.goto(url, wait_until="commit", timeout=60000)
            except:
                pass
        time.sleep(8)

        html = page.content()
        if "awsWafCookieDomainList" in html or "gokuProps" in html:
            raise ProxyError(
                "Amazon WAF bot challenge triggered — the site detected the request as automated. "
                "This usually means the browser's timezone/locale fingerprint doesn't match the proxy's location. "
                "geoip is auto-enabled when a proxy is provided to fix this. "
                "If still failing, try a residential proxy with matching timezone/locale."
            )

        dismiss_all_amazon_popups(page, cookie_timeout=5000)

        for selector in ["#productTitle", "#titleSection h1", ".product-title", "#price", "#availability"]:
            try:
                if page.locator(selector).count() > 0:
                    print(f"  Product content found: {selector}")
                    break
            except:
                pass
        else:
            print("  Product content not yet visible, waiting more...")
            time.sleep(5)

        dismiss_amazon_continue_shopping(page)

        asin = "default"
        m = re.search(r'/dp/([A-Z0-9]{10})', url, re.IGNORECASE)
        if m:
            asin = m.group(1)

        fullpage_path = capture_scroll_screenshot(
            page, save_dir, prefix=f"amazon_{asin}", max_scrolls=max_scrolls,
        )

        image_bytes = Path(fullpage_path).read_bytes()

        image_path = None
        if save_image:
            image_path = save_screenshot_to_disk(image_bytes, "amazon", asin)

        return {
            "site": "amazon",
            "url": url,
            "image_bytes": image_bytes,
            "image_path": image_path,
        }

    except Exception as e:
        print(f"Error capturing Amazon: {e}")
        import traceback
        traceback.print_exc()
        try:
            err_path = save_dir / "amazon_error.png"
            page.screenshot(path=str(err_path), full_page=False)
            image_bytes = Path(err_path).read_bytes()
            image_path = None
            if save_image:
                image_path = save_screenshot_to_disk(image_bytes, "amazon", "error")
            return {
                "site": "amazon",
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