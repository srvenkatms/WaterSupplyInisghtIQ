"""Durable 90-day disabled-account reference; no analytics-rule write access."""
from __future__ import annotations

import json
import uuid
from urllib.request import Request, urlopen


class WatchlistSink:
    def __init__(self, utilities: list[dict], credential):
        self.workspaces = {u["utilityId"]: u["workspaceResourceId"] for u in utilities}
        self.credential = credential

    def put_item(self, uid: str, alias: str, item_id: uuid.UUID, values: dict) -> None:
        url = (f"https://management.azure.com{self.workspaces[uid]}/providers/Microsoft.SecurityInsights/"
               f"watchlists/{alias}/watchlistItems/{item_id}?api-version=2024-03-01")
        token = self.credential.get_token("https://management.azure.com/.default").token
        payload = {"properties": {"itemsKeyValue": values}}
        request = Request(url, data=json.dumps(payload).encode(), method="PUT",
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            if response.status not in {200, 201}:
                raise RuntimeError(f"Watchlist {alias} reference update failed: {response.status}")


class AccountStateSink(WatchlistSink):
    def apply(self, uid: str, records: list[dict]) -> None:
        for record in sorted(records, key=lambda r: r["TimeGenerated"]):
            operation = record["OperationName"]
            if operation not in {"Disable account", "Enable account"} or record["Result"] != "success":
                continue
            upn = record["TargetUser"].lower()
            item_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{uid}:{upn}")
            self.put_item(uid, "DisabledAccounts", item_id, {
                "UserPrincipalName": upn, "UtilityId": uid,
                "DisabledAt": record["TimeGenerated"] if operation == "Disable account" else "",
            })
