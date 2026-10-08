from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any

from .broker.owner import EtoroOwnerBroker
from .config import PROJECT_ROOT, fixed_storage_root, load_profile, route_environment
from .domain import Environment
from .errors import TraderError
from .provisioning import OwnerAdminService
from .secrets import CredentialVault


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader-admin")
    parser.add_argument("--config", default=".env")
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap")
    provision = commands.add_parser("provision")
    provision.add_argument("--trader", required=True)
    provision.add_argument("--investment-usd", required=True)
    provision.add_argument("--portfolio-name", required=True)
    provision.add_argument("--token-name", required=True)
    provision.add_argument("--administrative-request-key", required=True)
    provision.add_argument("--child-config", required=True)
    commands.add_parser("list")
    mirror = commands.add_parser("reconcile-mirror")
    mirror.add_argument("--trader", required=True)
    mirror.add_argument("--actual-realized-pnl-usd", required=True)
    mirror.add_argument("--actual-committed-usd", required=True)
    health = mirror.add_mutually_exclusive_group(required=True)
    health.add_argument("--copy-healthy", action="store_true")
    health.add_argument("--copy-unhealthy", action="store_true")
    return parser


def _append_local_secrets(config_path: Path) -> dict[str, object]:
    text = config_path.read_text(encoding="utf-8")
    existing = {line.split("=", 1)[0].strip() for line in text.splitlines() if "=" in line}
    additions: list[str] = []
    if "TRADER_OWNER_SERVICE_CREDENTIAL" not in existing:
        additions.append(f"TRADER_OWNER_SERVICE_CREDENTIAL={secrets.token_urlsafe(36)}")
    if "TRADER_VAULT_MASTER_KEY" not in existing:
        additions.append(f"TRADER_VAULT_MASTER_KEY={secrets.token_urlsafe(48)}")
    if additions:
        with config_path.open("a", encoding="utf-8") as stream:
            if text and not text.endswith("\n"):
                stream.write("\n")
            stream.write("\n".join(additions) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(config_path, 0o600)
    return {"configured": True, "generated_secret_count": len(additions)}


def _child_path(filename: str) -> Path:
    candidate = Path(filename)
    if candidate.is_absolute() or len(candidate.parts) != 1:
        raise TraderError("CONFIG_INVALID", "child config must be a filename in the project root")
    path = (PROJECT_ROOT / candidate).resolve()
    if path.parent != PROJECT_ROOT.resolve():
        raise TraderError("CONFIG_INVALID", "child config escapes the fixed project root")
    return path


def _write_child_profile(
    *,
    path: Path,
    api_key: str,
    child_user_key: str,
    service_credential: str,
    routes: dict[str, str],
    timeout_seconds: float,
) -> None:
    lines = [
        *(f"{name}={url}" for name, url in sorted(routes.items())),
        f"ETORO_API_KEY={api_key}",
        f"ETORO_USER_KEY={child_user_key}",
        f"TRADER_SERVICE_CREDENTIAL={service_credential}",
        f"TRADER_HTTP_TIMEOUT_SECONDS={timeout_seconds:g}",
    ]
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise TraderError("CONFIG_CONFLICT", "child config already exists") from exc
    try:
        os.write(descriptor, ("\n".join(lines) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _dispatch(args: argparse.Namespace) -> Any:
    profile = load_profile(args.config)
    if args.command == "bootstrap":
        return _append_local_secrets(profile.path)
    environment = route_environment(profile)
    if environment is not Environment.DEMO:
        raise TraderError("ENVIRONMENT_ASSERTION_MISMATCH", "owner CLI is restricted to the demo profile")
    if not profile.owner_service_credential or not profile.vault_master_key:
        raise TraderError("CONFIG_INVALID", "run trader-admin bootstrap before owner operations")
    root = fixed_storage_root(environment)
    broker = EtoroOwnerBroker(profile)
    if args.command == "list":
        return {
            "portfolios": [
                {
                    "agent_portfolio_id": item.agent_portfolio_id,
                    "name": item.portfolio_name,
                    "agent_portfolio_gcid": item.agent_portfolio_gcid,
                    "virtual_balance_usd": str(item.virtual_balance_usd),
                    "mirror_id": item.mirror_id,
                    "scopes": sorted(item.scopes),
                }
                for item in broker.list_portfolios()
            ]
        }
    admin = OwnerAdminService(
        environment=environment,
        owner_identity=broker.owner_identity,
        owner_service_credential=profile.owner_service_credential,
        expected_owner_service_credential=profile.owner_service_credential,
        storage_root=root,
    )
    if args.command == "reconcile-mirror":
        return admin.record_mirror_reconciliation(
            trader_id=args.trader,
            actual_realized_pnl_usd=args.actual_realized_pnl_usd,
            actual_committed_usd=args.actual_committed_usd,
            copy_healthy=bool(args.copy_healthy),
        )
    service_credential = secrets.token_urlsafe(36)
    vault = CredentialVault(root / "credential-vault", profile.vault_master_key)
    result = admin.provision(
        trader_id=args.trader,
        service_credential=service_credential,
        investment_usd=args.investment_usd,
        portfolio_name=args.portfolio_name,
        token_name=args.token_name,
        administrative_request_key=args.administrative_request_key,
        broker=broker,
        vault=vault,
    )
    if result.get("state") != "READY":
        return result
    portfolio_id = str(result["agent_portfolio_id"])
    vault_reference = str(result["credential_reference"])
    child_key = vault.get(f"agent-portfolio:{portfolio_id}", vault_reference)
    child_path = _child_path(args.child_config)
    _write_child_profile(
        path=child_path,
        api_key=profile.api_key,
        child_user_key=child_key,
        service_credential=service_credential,
        routes={name: route.url for name, route in profile.routes.items()},
        timeout_seconds=profile.timeout_seconds,
    )
    return {
        "request_id": result["request_id"],
        "state": "READY",
        "trader_id": args.trader,
        "agent_portfolio_id": portfolio_id,
        "child_config": child_path.name,
    }


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--json" in raw:
        raw.remove("--json")
    request_id = str(uuid.uuid4())
    try:
        args = _parser().parse_args(raw)
        data = _dispatch(args)
        payload = {"schema_version": 1, "request_id": request_id, "status": "ok", "data": data, "error": None}
        print(json.dumps(payload, sort_keys=True, default=str))
        return 0
    except TraderError as exc:
        payload = {
            "schema_version": 1,
            "request_id": request_id,
            "status": "error",
            "data": None,
            "error": exc.as_dict(),
        }
        print(json.dumps(payload, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
