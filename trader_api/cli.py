from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

from .errors import TraderError
from .service import TraderService

VERSION = "0.2.0"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader")
    parser.add_argument("--config", default=".env")
    parser.add_argument("--trader")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--expected-environment", choices=("demo", "real"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("version", "capabilities", "status", "portfolio", "positions", "orders", "reconcile", "verify-audit"):
        commands.add_parser(name)
    init = commands.add_parser("init")
    init.add_argument("--expected-investment", required=True)
    init.add_argument("--currency", default="USD")
    for name in ("audit", "history", "trends"):
        command = commands.add_parser(name)
        command.add_argument("--limit", type=int, default=100)
    trade = commands.add_parser("trade")
    sub = trade.add_subparsers(dest="trade_command", required=True)
    opening = sub.add_parser("preview-open")
    instrument = opening.add_mutually_exclusive_group(required=True)
    instrument.add_argument("--symbol")
    instrument.add_argument("--instrument-id", type=int)
    opening.add_argument("--side", choices=("long", "short"), required=True)
    opening.add_argument("--order-type", choices=("market", "market_if_touched", "limit_ioc"), default="market")
    opening.add_argument("--strategy-notional-usd", required=True)
    opening.add_argument("--leverage", type=int, default=1)
    for name in ("trigger-rate", "limit-rate", "stop-loss-rate", "take-profit-rate"):
        opening.add_argument("--" + name)
    close = sub.add_parser("preview-close")
    close.add_argument("--position-id", required=True)
    close.add_argument("--fraction", default="1")
    modify = sub.add_parser("preview-modify")
    modify.add_argument("--position-id", required=True)
    modify.add_argument("--stop-loss-rate")
    modify.add_argument("--take-profit-rate")
    modify.add_argument("--stop-loss-type", choices=("rate", "trailing"))
    cancel = sub.add_parser("preview-cancel")
    cancel.add_argument("--order-id", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--preview-id", required=True)
    submit.add_argument("--idempotency-key", required=True)
    intent = sub.add_parser("status")
    intent.add_argument("--intent-id", required=True)
    return parser


def _reject_obsolete(argv: list[str]) -> None:
    migrations = {
        "--env": "use --expected-environment only as an equality assertion",
        "--balance": "use init --expected-investment for an owner-provisioned investment",
        "--notional": "use --strategy-notional-usd; broker amount is derived",
    }
    for argument, guidance in migrations.items():
        if argument in argv or any(item.startswith(argument + "=") for item in argv):
            raise TraderError("OBSOLETE_ARGUMENT", f"{argument} is obsolete; {guidance}")


def _dispatch(service: TraderService, args: argparse.Namespace) -> Any:
    direct = {
        "capabilities": service.capabilities,
        "status": service.trader_status,
        "portfolio": service.portfolio,
        "positions": service.positions,
        "orders": service.orders,
        "reconcile": service.reconcile,
        "verify-audit": service.verify_audit,
    }
    if args.command in direct:
        return direct[args.command]()
    if args.command == "init":
        return service.initialize(args.expected_investment, args.currency)
    if args.command == "audit":
        return service.audit_events(limit=args.limit)
    if args.command == "history":
        return service.portfolio_history(limit=args.limit)
    if args.command == "trends":
        return service.trends(limit=args.limit)
    if args.trade_command == "preview-open":
        return service.preview_open(
            symbol=args.symbol,
            instrument_id=args.instrument_id,
            side=args.side,
            order_type=args.order_type,
            strategy_notional_usd=args.strategy_notional_usd,
            leverage=args.leverage,
            trigger_rate=args.trigger_rate,
            limit_rate=args.limit_rate,
            stop_loss_rate=args.stop_loss_rate,
            take_profit_rate=args.take_profit_rate,
        )
    if args.trade_command == "preview-close":
        return service.preview_close(position_id=args.position_id, fraction=args.fraction)
    if args.trade_command == "preview-modify":
        return service.preview_modify(
            position_id=args.position_id,
            stop_loss_rate=args.stop_loss_rate,
            take_profit_rate=args.take_profit_rate,
            stop_loss_type=args.stop_loss_type,
        )
    if args.trade_command == "preview-cancel":
        return service.preview_cancel(order_id=args.order_id)
    if args.trade_command == "submit":
        return service.submit(args.preview_id, args.idempotency_key)
    return service.intent_status(args.intent_id)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw
    if json_requested:
        raw = [item for item in raw if item != "--json"]
    request_id = str(uuid.uuid4())
    trader_id: str | None = None
    environment: str | None = None
    try:
        _reject_obsolete(raw)
        args = _parser().parse_args(raw)
        args.json = json_requested
        trader_id = args.trader
        if args.command == "version":
            data: Any = {"version": VERSION}
        else:
            if not trader_id:
                raise TraderError("TRADER_MISMATCH", "--trader is required")
            service = TraderService(trader_id, args.config, args.expected_environment)
            environment = service.environment
            data = _dispatch(service, args)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "environment": environment,
                    "trader_id": trader_id,
                    "status": "ok",
                    "data": data,
                    "error": None,
                },
                sort_keys=True,
                default=str,
            )
        )
        return 0
    except TraderError as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "environment": environment,
                    "trader_id": trader_id,
                    "status": "error",
                    "data": None,
                    "error": exc.as_dict(),
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
