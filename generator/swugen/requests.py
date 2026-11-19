"""Validate scenario envelopes before storing or admitting them."""
import re
from datetime import datetime

from . import scenarios


def validate_request(request: dict, profiles: dict) -> None:
    if not isinstance(request, dict):
        raise ValueError("Scenario request must be an object")
    if request.get("utility") not in profiles or request.get("scenario") not in scenarios.EXPECTED:
        raise ValueError("Unknown utility or scenario")
    run_id = request.get("runId")
    if not isinstance(run_id, str) or not re.fullmatch(r"run-[A-Za-z0-9][A-Za-z0-9-]{0,100}", run_id):
        raise ValueError("Invalid scenario run ID")
    if not isinstance(request.get("start"), str):
        raise ValueError("Scenario start must be an ISO timestamp")
    start = datetime.fromisoformat(request["start"])
    if start.tzinfo is None:
        raise ValueError("Scenario start must include its UTC offset")
    if not scenarios.expected_rules(request["scenario"], profiles[request["utility"]])["applicable"]:
        raise ValueError("Scenario does not apply to this utility")
