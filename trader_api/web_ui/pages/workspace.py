"""NiceGUI workspace; memo handles and prepared commands stay in page closures."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from nicegui import run, ui

from ...domain import Side
from ...errors import TraderError
from ...storage import ExecutionState, Operation, PositionState
from ...ui_api import get_dashboard, list_operations, list_orders, list_positions, request_refresh
from ...ui_api.commands import (
    ActivationRequest,
    CancelOrderRequest,
    ClosePositionRequest,
    OpeningOrderType,
    OpenOrderRequest,
    activate,
    check_executions,
    get_command_availability,
    prepare_cancel,
    prepare_close,
    prepare_open,
)
from ..controllers.commands import CommandController
from ..formatting import format_money, format_time
from ..runtime import TraderRuntime


async def render_workspace(runtime: TraderRuntime, environment: str) -> None:
    session = runtime.session
    assert session is not None
    controller = CommandController(runtime)
    ui.context.client.on_disconnect(controller.leave)
    cursors: dict[str, Any] = {"Positions": None, "Pending orders": None, "History": None}
    next_cursors: dict[str, Any] = {}
    close_sizes: dict[str, float] = {}
    last_generation = -1
    price_labels: list[tuple[Any, Any]] = []
    initial_availability = await run.io_bound(get_command_availability, session)
    assert initial_availability is not None
    availability = initial_availability
    assert runtime.service is not None
    capabilities = runtime.service.broker.capabilities()

    def invalidate() -> None:
        controller.invalidate()
        review.clear()

    async def action(command: Any) -> Any:
        try:
            result = await command()
            if controller.active:
                await metrics.refresh()
            return result
        except Exception as exc:
            if controller.active:
                ui.notify(exc.code if isinstance(exc, TraderError) else "Action unavailable", type="negative")
            return None

    @ui.refreshable
    async def metrics() -> None:
        dashboard = await run.io_bound(get_dashboard, session)
        if dashboard is None or not controller.active:
            return
        with ui.row().classes("w-full gap-4 flex-wrap"):
            for label, value in (
                ("Allocated capital", dashboard.strategy_budget.initial_cap if dashboard.initialized else None),
                (
                    "Available strategy budget",
                    dashboard.strategy_budget.available_to_open if dashboard.initialized else None,
                ),
                ("Realized P/L", dashboard.valuation.realized_pnl),
                ("Unrealized P/L", dashboard.valuation.unrealized_pnl),
                ("Total P/L", dashboard.valuation.total_pnl),
            ):
                with ui.card().classes("metric flex-1 min-w-44 p-4 border border-slate-200 shadow-sm"):
                    ui.label(label).classes("text-sm text-slate-500")
                    ui.label(format_money(value, "USD")).classes("text-xl font-semibold")
        ui.label("Prices last refreshed: " + format_time(dashboard.valuation.observed_at)).classes(
            "text-red-700" if dashboard.valuation.price_stale else "text-slate-500"
        )
        if dashboard.refresh.error:
            ui.label("Price refresh unavailable; showing cached prices where available.").classes("text-amber-800")

    await metrics()
    with ui.row():

        async def refresh_prices() -> None:
            await run.io_bound(request_refresh, session)

        ui.button("Refresh prices", on_click=refresh_prices).props("outline")

        async def reconcile() -> None:
            result = await action(lambda: controller.execute(lambda: check_executions(session)))
            if result is not None:
                ui.notify(f"Executions checked: {result.unresolved_intents} pending or unknown")
                await records.refresh()

        button = ui.button("Check executions", on_click=reconcile).props("outline")
        button.set_enabled(availability.execution_reconciliation)
    if availability.activation:
        with ui.card().classes("w-full"):
            ui.label("Initialize strategy capital").classes("text-lg font-semibold")
            ui.label("Strategy capital is a local trading allocation, not a broker deposit.")
            amount = ui.number("Strategy capital (USD)", value=None, min=0.01).props("data-testid=capital")

            async def initialize() -> None:
                result = await action(
                    lambda: controller.execute(lambda: activate(session, ActivationRequest(Decimal(str(amount.value)))))
                )
                if result is not None:
                    ui.navigate.reload()

            ui.button("Initialize allocation", on_click=initialize)

    async def show_review(command: Any) -> None:
        review.clear()
        prepared = await action(lambda: controller.prepare(command))
        if prepared is None or not controller.active:
            return
        with review:
            ui.label("Review trade").classes("text-xl font-semibold")
            ui.label(f"Trader: {runtime.entry.label} · {environment.upper()} · {prepared.operation}")
            details = prepared.review
            labels = {
                "symbol": "Symbol",
                "instrument_id": "Instrument",
                "side": "Side",
                "order_type": "Order type",
                "strategy_notional_usd": "Size (USD)",
                "leverage": "Leverage",
                "fraction": "Close fraction",
                "units_to_deduct": "Close units",
                "trigger_rate": "Trigger price (USD)",
                "limit_rate": "Limit price (USD)",
                "stop_loss_rate": "Stop loss price (USD)",
                "take_profit_rate": "Take profit price (USD)",
                "position_id": "Position",
                "order_id": "Order",
                "estimated_strategy_cost_usd": "Estimated costs (USD)",
                "strategy_reservation_usd": "Budget reserved (USD)",
                "strategy_remaining_budget_usd": "Budget after opening (USD)",
            }
            for key, label in labels.items():
                if details.get(key) is not None:
                    ui.label(f"{label}: {details[key]}")
            if "estimated_strategy_cost_usd" not in details:
                ui.label("Estimated costs: Waiting USD · Budget impact confirmed on execution")
            ui.label("Review expires: " + format_time(prepared.expires_at))

            async def submit() -> None:
                if controller.busy:
                    return
                controller.busy = True
                submit_button.disable()
                try:
                    result = await action(controller.submit)
                    if result is not None and controller.active:
                        state = {
                            "FILLED": "Confirmed",
                            "CANCELED": "Confirmed cancellation",
                            "REJECTED": "Rejected",
                            "UNKNOWN": "Unknown — check executions",
                        }.get(result.state, "Pending — check executions")
                        status.set_text(state + (f" · {result.error_code}" if result.error_code else ""))
                        close_sizes.clear()
                        await records.refresh()
                finally:
                    controller.busy = False
                    if controller.active:
                        submit_button.enable()

            submit_button = ui.button("Submit trade", on_click=submit)
            status = ui.label("")

    with ui.expansion("Open order", value=True).classes("w-full border rounded-lg"):
        with ui.row().classes("w-full items-start"):
            selection = ui.select(["Symbol", "Instrument ID"], value="Symbol", label="Select by", on_change=invalidate)
            instrument = ui.input("Symbol or instrument ID", value="ETH", on_change=invalidate)
            order_type = ui.select(
                {
                    kind.value: {
                        "market": "Market",
                        "market_if_touched": "Market if touched",
                        "limit_ioc": "Limit IOC",
                    }[kind.value]
                    for kind in OpeningOrderType
                    if capabilities.get(kind.value)
                },
                value="market" if capabilities.get("market") else None,
                label="Order type",
                on_change=invalidate,
            )
            side = ui.select(["long", "short"], value="long", label="Side", on_change=invalidate)
            notional = ui.number("Notional (USD)", value=None, min=0.01, on_change=invalidate)
            leverage = ui.number("Leverage", value=1, min=1, step=1, on_change=invalidate)
        with ui.row():
            trigger = ui.number("Trigger price (USD)", value=None, on_change=invalidate)
            trigger.bind_visibility_from(order_type, "value", backward=lambda value: value == "market_if_touched")
            limit = ui.number("Limit price (USD)", value=None, on_change=invalidate)
            limit.bind_visibility_from(order_type, "value", backward=lambda value: value == "limit_ioc")
            stop = ui.number("Stop loss price (USD)", value=None, on_change=invalidate)
            take = ui.number("Take profit price (USD)", value=None, on_change=invalidate)

        async def opening() -> None:
            def optional(value: Any) -> Decimal | None:
                return None if value is None else Decimal(str(value))

            try:
                request = OpenOrderRequest(
                    instrument.value.strip() if selection.value == "Symbol" else None,
                    int(instrument.value) if selection.value == "Instrument ID" else None,
                    Side(side.value),
                    OpeningOrderType(order_type.value),
                    Decimal(str(notional.value)),
                    int(leverage.value),
                    optional(trigger.value) if order_type.value == "market_if_touched" else None,
                    optional(limit.value) if order_type.value == "limit_ioc" else None,
                    optional(stop.value),
                    optional(take.value),
                )
            except (ValueError, TypeError, ArithmeticError):
                ui.notify("Enter a valid instrument and positive USD size", type="negative")
                return
            await show_review(lambda: prepare_open(session, request))

        open_button = ui.button("Review opening", on_click=opening)
        open_button.set_enabled(availability.opening)
        if not availability.opening:
            ui.label("Opening unavailable: initialize allocation and verify trading permissions/capabilities.")
    review = ui.column().classes("w-full p-4 border rounded-lg")

    with ui.tabs().classes("w-full") as tabs:
        for name in cursors:
            ui.tab(name)

    async def move(name: str, cursor: Any) -> None:
        cursors[name] = cursor
        invalidate()
        await records.refresh()

    @ui.refreshable
    async def records() -> None:
        price_labels.clear()
        with ui.tab_panels(tabs, value=tabs.value or "Positions").classes("w-full"):
            for name in cursors:
                with ui.tab_panel(name):
                    page: Any
                    if name == "Positions":
                        page = await run.io_bound(
                            list_positions, session, state=PositionState.OPEN, limit=10, cursor=cursors[name]
                        )
                    elif name == "Pending orders":
                        page = await run.io_bound(
                            list_orders, session, state=ExecutionState.PENDING, limit=10, cursor=cursors[name]
                        )
                    else:
                        page = await run.io_bound(list_operations, session, limit=10, cursor=cursors[name])
                    if page is None:
                        continue
                    next_cursors[name] = page.next_cursor
                    if not page.items:
                        ui.label("No " + name.lower())
                    for item in page.items:
                        with ui.card().classes("w-full"):
                            if name == "Positions":
                                ui.label(f"{item.symbol or item.instrument_id} · {item.side} · {item.leverage}x")
                                ui.label(
                                    f"Notional: {format_money(item.strategy_notional_usd, 'USD')} · "
                                    f"Units: {item.remaining_units if item.remaining_units is not None else 'Waiting'}"
                                )
                                ui.label(
                                    f"Entry: {format_money(item.entry_price, 'USD')} · "
                                    f"Liquidation: {format_money(item.liquidation_price, 'USD')} · "
                                    f"P/L: {format_money(item.unrealized_pnl, 'USD')}"
                                )
                                price_label = ui.label(
                                    "Price refreshed: " + format_time(item.price_refreshed_at)
                                ).classes("text-red-700" if item.price_stale else "text-slate-500")

                                price_labels.append((price_label, item.price_refreshed_at))

                                def change_size(event: Any, key: str = item.position_id) -> None:
                                    close_sizes[key] = event.value
                                    invalidate()

                                fraction = ui.number(
                                    "Close size (%)",
                                    value=close_sizes.get(item.position_id, 100),
                                    min=0.01,
                                    max=100,
                                    on_change=change_size,
                                )

                                async def close(position: Any = item.position, size: Any = fraction) -> None:
                                    try:
                                        request = ClosePositionRequest(position, Decimal(str(size.value)) / 100)
                                    except ArithmeticError:
                                        ui.notify("Enter a close percentage", type="negative")
                                        return
                                    await show_review(lambda: prepare_close(session, request))

                                close_button = ui.button("Review close", on_click=close)
                                close_button.set_enabled(availability.close and item.remaining_units is not None)
                            elif name == "Pending orders":
                                ui.label(f"{item.symbol} · {item.state} · Order {item.order_id}")

                                async def cancel(order: Any = item.order) -> None:
                                    await show_review(lambda: prepare_cancel(session, CancelOrderRequest(order)))

                                cancel_button = ui.button("Review cancellation", on_click=cancel)
                                cancel_button.set_enabled(
                                    availability.cancel
                                    and capabilities.get(
                                        "cancel_close" if item.operation == Operation.close else "cancel", False
                                    )
                                )
                            else:
                                state = {
                                    "FILLED": "Confirmed",
                                    "CANCELED": "Confirmed cancellation",
                                    "UNKNOWN": "Unknown",
                                    "REJECTED": "Rejected",
                                }.get(str(item.state), "Pending")
                                ui.label(f"{item.operation} · {state} · {format_time(item.created_at)}")
                                ui.label(f"{item.params.get('symbol') or item.params.get('instrument_id', '')}")
                                if item.params.get("strategy_notional_usd") is not None:
                                    ui.label(format_money(item.params["strategy_notional_usd"], "USD"))
                                if item.error_code:
                                    ui.label(item.error_code).classes("text-red-700")
                    with ui.row():
                        ui.button("First page", on_click=lambda name=name: move(name, None)).props("outline")
                        next_button = ui.button("Next page", on_click=lambda name=name: move(name, next_cursors[name]))
                        next_button.set_enabled(page.next_cursor is not None)

    await records()

    async def poll() -> None:
        nonlocal availability, last_generation
        if not controller.active:
            return
        availability = await run.io_bound(get_command_availability, session) or availability
        open_button.set_enabled(availability.opening)
        dashboard = await run.io_bound(get_dashboard, session)
        if dashboard is not None and dashboard.refresh.generation != last_generation:
            last_generation = dashboard.refresh.generation
            await records.refresh()
        for label, stamp in price_labels:
            stale = stamp is not None and (session._clock() - stamp).total_seconds() > 90
            label.classes(
                add="text-red-700" if stale else "text-slate-500", remove="text-slate-500" if stale else "text-red-700"
            )
        await metrics.refresh()

    ui.timer(5, poll)
