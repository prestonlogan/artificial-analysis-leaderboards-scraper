"""
HTML content fetcher for the Artificial Analysis Leaderboard Scraper.

This module fetches HTML content from the target website using Playwright,
handling retry logic, rate limiting, and error handling.

Key Features:
- Fetches leaderboard HTML with Playwright and model comparison data directly
- Implements retry logic with exponential backoff
- Handles HTTP errors (404, 500, timeout, etc.)
"""

import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.sync_api import sync_playwright
from rich.console import Console

# Use the project-local browser directory for every Playwright entry point.
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path(__file__).resolve().parents[2] / ".browsers"),
)

console = Console()

LEADERBOARD_READY_SELECTORS = [
    "text=Comparison of Models:",
    "text=API Provider",
    "table",
    "thead",
    "tbody",
]

MODELS_MANIFEST_PATTERN = re.compile(
    r'\\?"path\\?":\\?"(/data/[^"\\]+\.txt)\\?",'
    r'\\?"key\\?":\\?"([0-9a-f]{64})'
)


class PlaywrightBrowserMissingError(RuntimeError):
    """Raised when the Playwright browser binary is not installed locally."""

def _is_missing_browser_error(exc: Exception) -> bool:
    """Return True when Playwright is installed but its browser binary is missing."""
    message = str(exc)
    return "Executable doesn't exist" in message and "playwright install" in message


def _wait_for_leaderboard_content(page, logger: logging.Logger) -> None:
    """Wait briefly for a rendered leaderboard before extracting page HTML."""
    wait_timeout_ms = 15000

    try:
        page.wait_for_function(
            """selectors => selectors.some(selector =>
                selector.startsWith("text=")
                    ? document.body?.innerText.includes(selector.slice(5))
                    : document.querySelector(selector)
            )""",
            LEADERBOARD_READY_SELECTORS,
            timeout=wait_timeout_ms,
        )
        logger.info("Detected page content")
        return
    except Exception:
        pass

    logger.warning(
        "Timed out waiting for leaderboard-specific content; falling back to current DOM snapshot"
    )


def _decrypt_manifest(url: str, key: str):
    """Download, decrypt, and decompress an Artificial Analysis data manifest."""
    with urlopen(f"{url}?_={time.time_ns()}", timeout=30) as response:
        encrypted = response.read()
    key_bytes = bytes.fromhex(key)
    iv = hashlib.sha256(key_bytes).digest()[:12]
    return json.loads(gzip.decompress(AESGCM(key_bytes).decrypt(iv, encrypted, None)))


def _embed_models_data(
    html: str, logger: logging.Logger, base_url: str = "https://artificialanalysis.ai"
) -> str:
    """Embed the comparison page's decrypted model records in its HTML."""
    for path, key in MODELS_MANIFEST_PATTERN.findall(html):
        try:
            data = _decrypt_manifest(urljoin(base_url, path), key)
        except Exception as exc:
            logger.warning("Failed to read model data manifest %s: %s", path, exc)
            continue

        if isinstance(data, dict) and isinstance(data.get("models"), list):
            payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
            marker = f'<script id="__MODELS_DATA__" type="application/json">{payload}</script>'
            logger.info("Extracted %s model records", len(data["models"]))
            return html.replace("</body>", f"{marker}</body>", 1)

    return html


def fetch_html_with_playwright(
    url: str, click_header_buttons: bool = True
) -> Optional[str]:
    """
    Fetch HTML content from a given URL using Playwright to render JavaScript.

    Args:
        url (str): The URL to fetch HTML content from
        click_header_buttons (bool): If True, attempt to click all buttons found in thead elements
                                    first <tr> to expand column headers before extracting HTML.

    Returns:
        Optional[str]: HTML content as string if successful, None otherwise
    """
    logger = logging.getLogger("web_scraper")
    try:
        with console.status(
            "[bold green]Rendering page with Playwright...", spinner="dots"
        ) as status:
            if urlparse(url).path.rstrip("/") == "/models":
                status.update("Fetching model data...")
                parsed_url = urlparse(url)
                manifest_url = (
                    f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
                    f"?_={time.time_ns()}"
                )
                with urlopen(manifest_url, timeout=30) as response:
                    html = response.read().decode("utf-8")
                html = _embed_models_data(html, logger, url)
                if '__MODELS_DATA__' not in html:
                    raise RuntimeError("Model data payload was not available")
                logger.info("Successfully fetched model comparison data")
                return html

            with sync_playwright() as p:
                status.update("Launching browser...")
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                status.update("Navigating to page...")
                response = page.goto(url)
                if response and not response.ok:
                    raise RuntimeError(f"HTTP {response.status} loading {url}")
                status.update("Waiting for page to load...")
                _wait_for_leaderboard_content(page, logger)

                headers_clicked = False
                if click_header_buttons:
                    status.update("Clicking headers...")
                    try:
                        header_buttons = page.locator("thead tr:first-of-type button")
                        btn_count = header_buttons.count()
                        logger.info(
                            f"Found {btn_count} header buttons in thead; attempting to click them"
                        )
                        for i in range(btn_count):
                            btn = header_buttons.nth(i)
                            try:
                                # Only click if visible/enabled
                                if btn.is_visible() and btn.is_enabled():
                                    btn.click()
                                    headers_clicked = True
                                    logger.debug(f"Clicked header button #{i}")
                                    # Small wait to allow DOM updates to settle
                                    page.wait_for_timeout(200)
                                else:
                                    logger.debug(
                                        f"Skipping header button #{i} (not visible or not enabled)"
                                    )
                            except Exception as click_exc:
                                logger.warning(
                                    f"Error clicking header button #{i}: {click_exc}"
                                )
                    except Exception as e:
                        logger.warning(f"Failed to locate or click header buttons: {e}")

                if headers_clicked:
                    page.wait_for_load_state("networkidle")
                status.update("Extracting HTML...")
                html = page.content()
                browser.close()
                logger.info("Successfully fetched HTML from %s", url.split("?", 1)[0])
                return html
    except Exception as e:
        if _is_missing_browser_error(e):
            raise PlaywrightBrowserMissingError(
                "Playwright is installed but no browser executable is available. "
                "Run `python src/main.py --install-browser` and retry."
            ) from e
        logger.error(
            "Failed to fetch HTML from %s using Playwright: %s",
            url.split("?", 1)[0],
            e,
        )
        return None


def fetch_html(url: str, retries: int = 3, delay: int = 5) -> Optional[str]:
    """
    Fetch HTML content from a given URL using Playwright as the primary method.

    Args:
        url (str): The URL to fetch HTML content from
        retries (int): Number of retry attempts (default: 3)
        delay (int): Base delay between retries in seconds (default: 5)

    Returns:
        Optional[str]: HTML content as string if successful, None otherwise
    """
    logger = logging.getLogger("web_scraper")
    display_url = url.split("?", 1)[0]

    for attempt in range(retries + 1):
        try:
            html = fetch_html_with_playwright(url)
        except PlaywrightBrowserMissingError as exc:
            logger.error(str(exc))
            return None

        if html is not None:
            return html

        if attempt < retries:
            backoff_delay = delay * (2**attempt)
            logger.warning(
                "Attempt %s failed fetching HTML from %s with Playwright. Retrying in %s seconds...",
                attempt + 1,
                display_url,
                backoff_delay,
            )
            time.sleep(backoff_delay)
        else:
            logger.error(
                "Failed to fetch HTML from %s after %s attempts with Playwright",
                display_url,
                retries + 1,
            )
            return None
