from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx

from ..auth import fingerprint
from ..config import Profile
from ..domain import decimal_value, money
from ..errors import TraderError
from ..provisioning import ProvisionedPortfolio

CREATE_URL = "https://public-api.etoro.com/api/v2/agent-portfolios"
LIST_URL = "https://public-api.etoro.com/api/v1/agent-portfolios"


class EtoroOwnerBroker:
    """Owner-only Agent Portfolio adapter with hard-pinned operation URLs."""

    def __init__(self, profile: Profile, client: httpx.Client | None = None) -> None:
        self.profile = profile
        self.client = client or httpx.Client(timeout=profile.timeout_seconds, follow_redirects=False)
        # The public create contract does not expose the owner's account id. This
        # non-secret identifier is only a local owner-credential binding.
        self.owner_identity = f"credential:{fingerprint(profile.user_key)}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        # Locally generated correlation UUID sent as eToro x-request-id, not a memo ID.
        request_id: str,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {
            "x-api-key": self.profile.api_key,
            "x-user-key": self.profile.user_key,
            "x-request-id": request_id,
            "accept": "application/json",
        }
        try:
            response = self.client.request(method, url, headers=headers, json=json_body)
        except httpx.TimeoutException as exc:
            raise TraderError("BROKER_OUTCOME_UNKNOWN", "owner broker request timed out") from exc
        except httpx.HTTPError as exc:
            raise TraderError("BROKER_UNAVAILABLE", "owner broker transport failed", retryable=True) from exc
        if response.status_code == 429:
            raise TraderError("BROKER_RATE_LIMITED", "owner broker rate limit reached", retryable=True)
        if response.status_code in {401, 403}:
            raise TraderError("PERMISSION_REVOKED", "broker rejected the owner credential")
        if response.is_redirect:
            raise TraderError("BROKER_PROTOCOL_ERROR", "broker redirects are not accepted")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TraderError("BROKER_PROTOCOL_ERROR", "owner broker returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise TraderError("BROKER_PROTOCOL_ERROR", "owner broker response must be an object")
        return response.status_code, payload

    def create_portfolio(
        self,
        *,
        # Locally generated correlation UUID sent as eToro x-request-id, not a memo ID.
        request_id: str,
        investment_usd: Decimal,
        name: str,
        token_name: str,
        scopes: frozenset[str],
    ) -> ProvisionedPortfolio:
        status, payload = self._request(
            "POST",
            CREATE_URL,
            request_id=request_id,
            json_body={
                "investmentAmountInUsd": str(investment_usd),
                "agentPortfolioName": name,
                "agentPortfolioDescription": "Isolated CLI demo portfolio",
                "userTokenName": token_name,
                "scopeNames": sorted(scopes),
            },
        )
        # 207 means the funded portfolio exists but its one-time token failed.
        # Treat it as an ambiguous mutation so provisioning is never repeated.
        if status == 207:
            raise TraderError(
                "BROKER_OUTCOME_UNKNOWN",
                "portfolio was created but child-token provisioning needs owner repair",
            )
        if status != 201:
            if status >= 500:
                raise TraderError("BROKER_OUTCOME_UNKNOWN", "owner broker returned an ambiguous server error")
            raise TraderError(
                "BROKER_REJECTED",
                "owner broker rejected portfolio provisioning",
                details={"status_code": status},
            )
        try:
            portfolio_id = str(uuid.UUID(str(payload["agentPortfolioId"])))
            gcid = str(int(payload["agentPortfolioGcid"]))
            virtual_balance = money(payload["agentPortfolioVirtualBalance"])
            mirror_id = str(int(payload["mirrorId"]))
            tokens = payload["userTokens"]
            if not isinstance(tokens, list) or len(tokens) != 1 or not isinstance(tokens[0], dict):
                raise ValueError
            token = tokens[0]
            child_user_key = str(token["userToken"])
            raw_scopes = token["scopes"]
            if not isinstance(raw_scopes, list):
                raise ValueError
            issued_scopes = frozenset(str(item["name"] if isinstance(item, dict) else item) for item in raw_scopes)
        except (KeyError, TypeError, ValueError) as exc:
            raise TraderError(
                "BROKER_OUTCOME_UNKNOWN",
                "created portfolio response lacks required one-time binding data",
            ) from exc
        if not child_user_key:
            raise TraderError("BROKER_OUTCOME_UNKNOWN", "created portfolio response lacks the child token")
        return ProvisionedPortfolio(
            owner_account_id=self.owner_identity,
            agent_portfolio_id=portfolio_id,
            agent_portfolio_gcid=gcid,
            # The create response binds this exact child token to these two
            # immutable portfolio identifiers. The trading PnL schema exposes
            # no separate account/portfolio ids, so these are the authoritative
            # issuance identifiers retained as verification evidence.
            agent_trading_account_id=gcid,
            agent_trading_portfolio_id=portfolio_id,
            mirror_id=mirror_id,
            investment_usd=money(investment_usd),
            virtual_balance_usd=virtual_balance,
            child_user_key=child_user_key,
            scopes=issued_scopes,
            portfolio_name=str(payload.get("agentPortfolioName", name)),
        )

    def list_portfolios(self) -> list[ProvisionedPortfolio]:
        status, payload = self._request("GET", LIST_URL, request_id=str(uuid.uuid4()))
        if status != 200:
            raise TraderError(
                "BROKER_REJECTED",
                "owner broker rejected portfolio listing",
                details={"status_code": status},
            )
        raw_items = payload.get("agentPortfolios")
        if not isinstance(raw_items, list):
            raise TraderError("BROKER_PROTOCOL_ERROR", "portfolio listing lacks agentPortfolios")
        result: list[ProvisionedPortfolio] = []
        try:
            for item in raw_items:
                if not isinstance(item, dict):
                    raise ValueError
                raw_tokens = item.get("userTokens", [])
                scopes = frozenset(
                    str(scope)
                    for token in raw_tokens
                    if isinstance(token, dict)
                    for scope in token.get("scopeNames", [])
                )
                portfolio_id = str(uuid.UUID(str(item["agentPortfolioId"])))
                gcid = str(int(item["agentPortfolioGcid"]))
                result.append(
                    ProvisionedPortfolio(
                        self.owner_identity,
                        portfolio_id,
                        gcid,
                        gcid,
                        portfolio_id,
                        str(int(item["mirrorId"])),
                        Decimal("0"),
                        money(decimal_value(item["agentPortfolioVirtualBalance"])),
                        "",
                        scopes,
                        str(item.get("agentPortfolioName", "")),
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise TraderError("BROKER_PROTOCOL_ERROR", "portfolio listing is malformed") from exc
        return result
