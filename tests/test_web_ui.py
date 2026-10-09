from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from trader_api.errors import TraderError
from trader_api.web_ui.catalog import CatalogEntry
from trader_api.web_ui.runtime import RuntimeState, TraderRuntime


@pytest.mark.parametrize(
    "error", [TraderError("CONFIG_INVALID", "profile is missing"), RuntimeError("broken database")]
)
def test_startup_errors_are_logged(error: Exception, caplog: pytest.LogCaptureFixture) -> None:
    runtime = TraderRuntime(CatalogEntry("alpha", "Demo trader", "alpha", ".env_demo", "standalone"))

    def fail(_entry: CatalogEntry) -> None:
        raise error

    with caplog.at_level(logging.ERROR):
        runtime.start(fail, "demo")
    assert runtime.state == RuntimeState.FAILED_STARTUP
    assert not runtime.accepting_commands
    assert runtime.error_code == (error.code if isinstance(error, TraderError) else "STARTUP_FAILED")
    assert "Trader alpha initialization failed" in caplog.text
    assert str(error) in caplog.text
    assert caplog.records[-1].exc_info is not None
    runtime.stop()


def test_browser_startup_navigation_and_refresh(tmp_path: Path) -> None:
    """Exercise real NiceGUI/asyncio and dbzero, with an isolated observational broker."""
    playwright = pytest.importorskip("playwright.sync_api")
    pytest.importorskip("nicegui")
    root = Path("/dbzero-data/trader-dev/tests") / uuid.uuid4().hex
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    code = """
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import trader_api.config as config
import trader_api.web_ui.runtime as runtime
from trader_api.service import TraderService
from tests.simulated_broker import SimulatedBroker
from trader_api.web_ui.main import main

config.PROJECT_ROOT = Path(sys.argv[1])
profile = config.PROJECT_ROOT / '.env_demo'
profile.write_text('ETORO_API_KEY=test-api\\nETORO_USER_KEY=test-user\\n'
                   'TRADER_TRADING_MODE=standalone\\n'
                   'ETORO_PNL_URL=https://public-api.etoro.com/api/v1/trading/info/demo/pnl\\n')
profile.chmod(0o600)
class Broker(SimulatedBroker):
    def __init__(self, trader):
        super().__init__()
        self.limited = trader == 'beta'
        if self.limited:
            self.next_id = 2000
    def capabilities(self):
        caps = super().capabilities()
        if self.limited:
            caps.update(partial_close=False, cancel=False, limit_ioc=False)
        return caps
from trader_api.web_ui.catalog import Catalog, CatalogEntry
import importlib
web_main = importlib.import_module('trader_api.web_ui.main')
web_main.default_catalog = lambda env: Catalog(1, env, (
    CatalogEntry('alpha', 'Alpha trader', 'alpha', '.env_demo', 'standalone'),
    CatalogEntry('beta', 'Beta trader', 'beta', '.env_demo', 'standalone')))
service_type = TraderService
runtime.TraderService = lambda trader, profile, **kwargs: service_type(
    trader, profile, **kwargs, broker=Broker(trader), storage_root=Path(sys.argv[2]))
main(['--host', '127.0.0.1', '--port', sys.argv[3]])
"""
    log = tmp_path / "server.log"
    server_env = os.environ.copy()
    server_env["PYTHONFAULTHANDLER"] = "1"
    # Run a standalone server rather than NiceGUI's pytest screen fixture.
    server_env.pop("PYTEST_CURRENT_TEST", None)
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path), str(root), str(port)],
            stdout=output,
            stderr=subprocess.STDOUT,
            env=server_env,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert process.poll() is None, log.read_text()
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail("UI server did not listen: " + log.read_text())
            with playwright.sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    page = browser.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{port}/")
                    playwright.expect(page.get_by_text("Running", exact=True).first).to_be_visible()
                    playwright.expect(page.get_by_role("button", name="Open workspace").first).to_be_enabled()
                    page.get_by_role("button", name="Open workspace").first.click()
                    page.wait_for_url("**/traders/alpha/overview")
                    playwright.expect(page.get_by_text("direct broker account verified", exact=False)).to_be_visible()
                    page.reload()  # also verify a direct overview load after the collector publishes
                    playwright.expect(page.get_by_role("button", name="Review opening")).to_be_disabled()
                    page.get_by_label("Strategy capital (USD)", exact=True).fill("1000")
                    page.get_by_role("button", name="Initialize allocation").click()
                    playwright.expect(page.get_by_role("button", name="Review opening")).to_be_enabled()
                    playwright.expect(page.get_by_text("1,000.00 USD", exact=True).first).to_be_visible()
                    playwright.expect(page.get_by_text("0.00 USD", exact=True).first).to_be_visible()
                    page.get_by_label("Notional (USD)", exact=True).fill("100")
                    page.get_by_role("button", name="Review opening").click()
                    playwright.expect(page.get_by_text("Review trade", exact=True)).to_be_visible()
                    page.get_by_label("Notional (USD)", exact=True).fill("110")
                    playwright.expect(page.get_by_role("button", name="Submit trade")).to_have_count(0)
                    page.get_by_role("button", name="Review opening").click()
                    page.get_by_role("button", name="Submit trade").click()
                    playwright.expect(page.get_by_text("Confirmed", exact=True)).to_be_visible()
                    playwright.expect(page.get_by_role("button", name="Review close")).to_be_enabled()
                    page.get_by_label("Close size (%)", exact=True).fill("50")
                    page.get_by_role("button", name="Review close").click()
                    page.get_by_role("button", name="Submit trade").click()
                    playwright.expect(page.get_by_text("Confirmed", exact=True)).to_be_visible()
                    page.get_by_role("button", name="Review close").click()
                    page.get_by_role("button", name="Submit trade").click()
                    playwright.expect(page.get_by_text("No positions", exact=True)).to_be_visible()
                    page.get_by_label("Order type", exact=True).click()
                    page.get_by_role("option", name="Market if touched", exact=True).click()
                    page.get_by_label("Trigger price (USD)", exact=True).fill("90")
                    page.get_by_role("button", name="Review opening").click()
                    page.get_by_role("button", name="Submit trade").click()
                    playwright.expect(page.get_by_text("Pending — check executions", exact=True)).to_be_visible()
                    page.get_by_role("tab", name="Pending orders", exact=True).click()
                    page.get_by_role("button", name="Review cancellation").click()
                    page.get_by_role("button", name="Submit trade").click()
                    playwright.expect(page.get_by_text("Confirmed cancellation", exact=True)).to_be_visible()
                    page.get_by_label("Order type", exact=True).click()
                    page.get_by_role("option", name="Limit IOC", exact=True).click()
                    page.get_by_label("Limit price (USD)", exact=True).fill("100")
                    page.get_by_role("button", name="Review opening").click()
                    page.get_by_role("button", name="Submit trade").click()
                    playwright.expect(page.get_by_text("Confirmed", exact=True)).to_be_visible()
                    page.get_by_role("button", name="Switch trader").click()
                    page.wait_for_url(f"http://127.0.0.1:{port}/")
                    playwright.expect(page.get_by_text("Running", exact=True).first).to_be_visible()
                    page.get_by_role("button", name="Open workspace").first.click()
                    page.wait_for_url("**/traders/alpha/overview")
                    page.get_by_label("Notional (USD)", exact=True).fill("100")
                    page.get_by_role("button", name="Review opening").click()
                    playwright.expect(page.get_by_role("button", name="Submit trade")).to_be_visible()
                    page.get_by_role("button", name="Switch trader").click()
                    page.get_by_role("button", name="Open workspace").nth(1).click()
                    page.wait_for_url("**/traders/beta/overview")
                    playwright.expect(page.get_by_role("button", name="Submit trade")).to_have_count(0)
                    playwright.expect(page.get_by_text("No positions", exact=True)).to_be_visible()
                    page.get_by_label("Strategy capital (USD)", exact=True).fill("500")
                    page.get_by_role("button", name="Initialize allocation").click()
                    playwright.expect(page.get_by_role("button", name="Review opening")).to_be_enabled()
                    page.get_by_label("Notional (USD)", exact=True).fill("100")
                    page.get_by_role("button", name="Review opening").click()
                    page.get_by_role("button", name="Submit trade").click()
                    playwright.expect(page.get_by_text("Confirmed", exact=True)).to_be_visible()
                    playwright.expect(page.get_by_role("button", name="Review close")).to_be_disabled()
                    page.get_by_label("Order type", exact=True).click()
                    playwright.expect(page.get_by_role("option", name="Limit IOC", exact=True)).to_have_count(0)
                    page.get_by_role("option", name="Market if touched", exact=True).click()
                    page.get_by_label("Trigger price (USD)", exact=True).fill("90")
                    page.get_by_role("button", name="Review opening").click()
                    page.get_by_role("button", name="Submit trade").click()
                    playwright.expect(page.get_by_text("Pending — check executions", exact=True)).to_be_visible()
                    page.get_by_role("tab", name="Pending orders", exact=True).click()
                    playwright.expect(page.get_by_role("button", name="Review cancellation")).to_be_disabled()
                    assert not errors
                finally:
                    browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            shutil.rmtree(root, ignore_errors=True)
    assert "ERROR" not in log.read_text(), log.read_text()


def test_disconnected_workspace_does_not_cancel_dispatched_command():
    import asyncio
    from threading import Event

    from trader_api.web_ui.controllers.commands import CommandController

    runtime = TraderRuntime(CatalogEntry("alpha", "Alpha", "alpha", ".env_demo", "standalone"))
    runtime.accepting_commands = True
    controller = CommandController(runtime)
    entered, release, finished = Event(), Event(), Event()

    def trade():
        entered.set()
        assert release.wait(5)
        finished.set()
        return "confirmed"

    async def exercise():
        waiter = asyncio.create_task(controller.execute(trade))
        assert await asyncio.to_thread(entered.wait, 3)
        controller.leave()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
        with pytest.raises(TraderError, match="no longer active"):
            await controller.execute(trade)

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        runtime.command_executor.shutdown(wait=True)
