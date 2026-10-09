"""Command-line entry point for the optional NiceGUI trader workspace."""

from __future__ import annotations

import argparse
import logging

from .catalog import default_catalog, load_catalog
from .runtime import RuntimeRegistry, RuntimeState

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader-ui")
    parser.add_argument(
        "--catalog",
        default=None,
        help="server-side trader catalog (default: built-in single-trader catalog)",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="use the verified REAL environment (default: demo)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="bind address (default: 0.0.0.0 for container deployments)",
    )
    parser.add_argument("--port", type=int, default=8080)
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    environment = "real" if args.real else "demo"
    if args.catalog is None:
        catalog = default_catalog(environment)
    else:
        catalog = load_catalog(args.catalog, expected_environment=environment)
    registry = RuntimeRegistry(catalog, environment=environment)
    try:
        try:
            from nicegui import run, ui
        except ImportError as exc:
            raise SystemExit("NiceGUI is required for trader-ui; install the 'ui' extra") from exc

        ui.add_head_html(
            """
            <style>
              body { background: #f8fafc; }
              .ui-shell { max-width: 1180px; margin: 0 auto; width: 100%; }
              .metric { min-height: 112px; }
            </style>
            """,
            shared=True,
        )

        def shell_header() -> None:
            with ui.header().classes("items-center gap-3 bg-white text-slate-900 shadow-sm px-6"):
                ui.icon("public" if environment == "real" else "science").classes(
                    "text-2xl text-red-700" if environment == "real" else "text-2xl text-blue-700"
                )
                ui.label("Trader workspace").classes("text-lg font-semibold")
                ui.badge(
                    "REAL" if environment == "real" else "DEMO",
                    color="negative" if environment == "real" else "primary",
                ).props("aria-label=Environment")

        async def open_trader(key: str) -> None:
            try:
                runtime = await run.io_bound(registry.open, key)
                if runtime is None:  # NiceGUI returns None when the worker await is cancelled.
                    return
            except Exception:
                logger.exception("Unable to open trader %s", key)
                ui.notify("Unable to open trader", type="negative")
                return
            if runtime.state.value == "FAILED_STARTUP":
                ui.notify(f"Trader unavailable: {runtime.error_code or 'STARTUP_FAILED'}", type="negative")
                return
            ui.navigate.to(f"/traders/{key}/overview")

        @ui.page("/")
        async def selection() -> None:
            shell_header()
            with ui.column().classes("ui-shell p-6 gap-6"):
                ui.label("Select a trader").classes("text-3xl font-semibold text-slate-900")
                ui.label("Open a configured workspace to inspect its local portfolio and activity.").classes(
                    "text-slate-600"
                )
                with ui.row().classes("w-full gap-5 flex-wrap"):
                    for entry in catalog.entries:
                        runtime = (
                            await run.io_bound(registry.open, entry.key)
                            if entry.trading_mode == "standalone"
                            else registry.get(entry.key)
                        )
                        state = "Not started" if runtime is None else runtime.state.value.replace("_", " ").title()
                        with ui.card().classes("w-80 p-5 border border-slate-200 shadow-sm"):
                            with ui.row().classes("items-center justify-between w-full"):
                                ui.label(entry.label).classes("text-lg font-semibold")
                                ui.badge(state, color="positive" if state == "Running" else "grey-7")
                            ui.label(f"Catalog key: {entry.key}").classes("text-sm text-slate-500")
                            ui.label(
                                f"{entry.trading_mode.title()} account · "
                                + (runtime.error_code if runtime and runtime.error_code else state)
                            ).classes("text-sm text-slate-500")
                            ui.button(
                                "Open workspace",
                                icon="arrow_forward",
                                on_click=lambda key=entry.key: open_trader(key),
                            ).classes("w-full mt-4").props(
                                "disable"
                                if entry.trading_mode == "standalone"
                                and (runtime is None or runtime.state != RuntimeState.RUNNING)
                                else ""
                            )

        @ui.page("/traders/{trader_key}/overview")
        async def overview(trader_key: str) -> None:
            shell_header()
            with ui.column().classes("ui-shell p-6 gap-6"):
                try:
                    entry = catalog.by_key(trader_key)
                    runtime = await run.io_bound(registry.open, trader_key)
                    if runtime is None:
                        return
                except Exception:
                    logger.exception("Unable to load trader %s", trader_key)
                    ui.notify("Trader is unavailable", type="negative")
                    ui.button("Back to trader selection", on_click=lambda: ui.navigate.to("/"))
                    return
                with ui.row().classes("items-center justify-between w-full"):
                    with ui.column().classes("gap-1"):
                        ui.label(entry.label).classes("text-3xl font-semibold text-slate-900")
                        ui.label(f"{entry.key} · {runtime.state.value.replace('_', ' ').title()}").classes(
                            "text-slate-500"
                        )
                    ui.button("Switch trader", icon="swap_horiz", on_click=lambda: ui.navigate.to("/")).props("outline")
                if runtime.state.value != "RUNNING" or runtime.session is None:
                    with ui.card().classes("w-full bg-red-50 border border-red-200"):
                        ui.label(f"Trader startup failed: {runtime.error_code or 'STARTUP_FAILED'}").classes(
                            "text-red-800"
                        )
                    return
                ui.label(
                    f"{entry.trading_mode.title()} readiness: "
                    + (
                        "direct broker account verified"
                        if entry.trading_mode == "standalone"
                        else "agent portfolio verified"
                    )
                ).classes("text-sm text-slate-600")
                from .pages.workspace import render_workspace

                await render_workspace(runtime, environment)

        ui.run(host=args.host, port=args.port, reload=False)
    finally:
        registry.shutdown()


if __name__ == "__main__":
    main()
