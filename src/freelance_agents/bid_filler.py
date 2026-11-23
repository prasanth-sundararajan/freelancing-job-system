"""Open a Freelancer project in a visible Chrome window and fill in the bid form.

It fills amount, delivery days and proposal text, then stops. It NEVER clicks
"Place Bid": you review the form and click it yourself. That keeps every bid a
human action, in line with Freelancer's ban on automatic bidders.

Uses your installed Chrome with a separate profile (data/browser_profile), so
you sign in to Freelancer there once. The window stays open between bids.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

AMOUNT, PERIOD, DESCRIPTION = "#bidAmountInput", "#periodInput", "#descriptionTextArea"
LOGIN_URL = "https://www.freelancer.com/login"


class SignInNeeded(Exception):
    pass


CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


class BidFiller:
    """headless=True (tests): Playwright-owned Chrome. Otherwise Chrome runs as its own process
    with a local debugging port, so it (and your Freelancer sign-in) survives bot restarts."""

    def __init__(self, profile_dir: str | Path, channel: str = "chrome", headless: bool = False, debug_port: int = 9333):
        self.profile_dir = str(profile_dir)
        self.channel = channel
        self.headless = headless
        self.debug_port = debug_port
        self._pw = None
        self._browser = None
        self._ctx = None

    def _alive(self) -> bool:
        try:
            if self._browser is not None:
                return self._browser.is_connected() and bool(self._browser.contexts)
            return self._ctx is not None and (self._ctx.pages is not None)
        except Exception:
            return False

    def _context(self):
        if self._ctx is not None and self._alive():
            return self._ctx
        self._ctx = self._browser = None
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        if self.headless:
            self._ctx = self._pw.chromium.launch_persistent_context(
                self.profile_dir, channel=self.channel, headless=True, no_viewport=True)
            return self._ctx
        endpoint = f"http://127.0.0.1:{self.debug_port}"
        try:
            self._browser = self._pw.chromium.connect_over_cdp(endpoint, timeout=3000)
        except Exception:
            self._start_chrome()
            for _ in range(30):
                time.sleep(0.5)
                try:
                    self._browser = self._pw.chromium.connect_over_cdp(endpoint, timeout=3000)
                    break
                except Exception:
                    continue
            else:
                raise RuntimeError("couldn't start or connect to the bot's Chrome")
        self._ctx = self._browser.contexts[0]
        return self._ctx

    def _start_chrome(self) -> None:
        chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
        if chrome is None:
            raise RuntimeError("Google Chrome not found")
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([chrome, f"--remote-debugging-port={self.debug_port}", "--remote-debugging-address=127.0.0.1",
                          f"--user-data-dir={self.profile_dir}", "--no-first-run", "--no-default-browser-check",
                          "--start-maximized", "about:blank"],
                         creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)

    def fill(self, url: str, amount: float, days: int, text: str, timeout_ms: int = 30000,
             settle_ms: int = 2000, attempts: int = 3) -> str:
        """Fill amount, days and proposal, then read them back to make sure they stuck.

        Freelancer's page finishes loading (hydrates) after the form first appears and can
        wipe values typed too early, so we wait for it to settle and re-fill anything cleared.
        Raises SignInNeeded on the login page, RuntimeError if the form never holds the values.
        """
        ctx = self._context()
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if self.signed_out(page):
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.bring_to_front()
            raise SignInNeeded("the bot's Chrome window isn't signed in to Freelancer. It now shows the login page: "
                               "sign in there, then tap Fill again.")
        try:
            for sel in (AMOUNT, PERIOD, DESCRIPTION):
                page.wait_for_selector(sel, state="visible", timeout=timeout_ms)
        except Exception:
            raise RuntimeError("the bid form didn't appear (project may be closed, or you already bid on it).")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass  # some pages keep polling; settle time below still applies
        page.wait_for_timeout(settle_ms)

        wanted = {AMOUNT: f"{amount:g}", PERIOD: str(int(days)), DESCRIPTION: text}
        for _ in range(attempts):
            for sel, value in wanted.items():
                if not self._holds(page, sel, value):
                    page.fill(sel, value)
                    page.locator(sel).dispatch_event("blur")
            page.wait_for_timeout(settle_ms)
            if all(self._holds(page, sel, value) for sel, value in wanted.items()):
                page.locator(DESCRIPTION).scroll_into_view_if_needed()
                page.bring_to_front()
                return page.url
        missing = [sel.lstrip("#") for sel, value in wanted.items() if not self._holds(page, sel, value)]
        raise RuntimeError(f"The page kept clearing: {', '.join(missing)}")

    @staticmethod
    def signed_out(page) -> bool:
        """Freelancer's header shows a 'Log In' link only when signed out."""
        if "/login" in page.url:
            return True
        try:
            return page.get_by_role("link", name="Log In", exact=True).count() > 0
        except Exception:
            return False

    def sign_in(self, timeout_s: int = 600) -> bool:
        """Open the login page and wait (up to timeout_s) until you're signed in."""
        page = self._context().new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.bring_to_front()
        for _ in range(timeout_s // 3):
            page.wait_for_timeout(3000)
            if "/login" not in page.url and not self.signed_out(page):
                return True
        return False

    @staticmethod
    def _holds(page, selector: str, value: str) -> bool:
        current = page.input_value(selector).strip()
        if selector == DESCRIPTION:
            return current == value.strip()
        try:  # amount may be reformatted, e.g. "15000" -> "15000.00"
            return float(current) == float(value)
        except ValueError:
            return False

    def close(self) -> None:
        """Disconnect. The separately started Chrome (and your sign-in) keeps running."""
        try:
            if self._browser is None and self._ctx is not None:
                self._ctx.close()  # headless test browser: really close it
        finally:
            if self._pw:
                self._pw.stop()
            self._ctx = self._browser = self._pw = None
