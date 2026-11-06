"""Local lab-run metadata; never grants a PLC write capability."""
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="")
    parser.add_argument("--suppress-until")
    args = parser.parse_args()
    if args.run_id and (not args.run_id.startswith("run-") or not args.run_id.replace("-", "").isalnum()):
        raise ValueError("Invalid lab run ID")
    if args.suppress_until:
        until = datetime.fromisoformat(args.suppress_until)
        if until.tzinfo is None or not 0 < (until - datetime.now(timezone.utc)).total_seconds() <= 5400:
            raise ValueError("Suppression must end within the lab scenario's 90-minute limit")
    path = Path(os.environ.get("SWU_STATE_DIR", "/state")) / "control.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"runId": args.run_id, "suppressUntil": args.suppress_until}), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    main()
