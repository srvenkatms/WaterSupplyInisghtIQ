import json

import swugen.account_state as account_state


class Credential:
    def get_token(self, scope):
        assert scope == "https://management.azure.com/.default"
        return type("Token", (), {"token": "synthetic-test-token"})()


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_account_updates_are_idempotent_and_reenable_clears_disable_time(monkeypatch):
    requests = []

    def send(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr(account_state, "urlopen", send)
    sink = account_state.AccountStateSink([{"utilityId": "uta", "workspaceResourceId": "/synthetic/workspace"}],
                                         Credential())
    disable = {"OperationName": "Disable account", "Result": "success", "TargetUser": "leaver@uta.example",
               "TimeGenerated": "2026-11-02T00:00:00+00:00"}
    enable = disable | {"OperationName": "Enable account", "TimeGenerated": "2026-11-03T00:00:00+00:00"}
    sink.apply("uta", [disable])
    sink.apply("uta", [disable])
    sink.apply("uta", [enable])
    assert len({request.full_url for request in requests}) == 1
    assert json.loads(requests[0].data)["properties"]["itemsKeyValue"]["DisabledAt"] == disable["TimeGenerated"]
    assert json.loads(requests[-1].data)["properties"]["itemsKeyValue"]["DisabledAt"] == ""
    assert "/watchlists/DisabledAccounts/watchlistItems/" in requests[0].full_url
