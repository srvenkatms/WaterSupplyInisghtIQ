"""Relay genuine lab action audit records, not native Entra/Firewall/Defender logs."""
import json
import logging
import os
import time

from azure.identity import ManagedIdentityCredential
from azure.storage.queue import QueueClient
from swugen.sink import IngestionSink


def main():
    logging.basicConfig(level=logging.INFO)
    credential = ManagedIdentityCredential()
    route = os.environ["SWU_ROUTE"]
    queue = QueueClient(f"https://{os.environ['SWU_STORAGE_ACCOUNT']}.queue.core.windows.net",
                        f"{route}-audit", credential)
    sink = IngestionSink(os.environ["SWU_DCE_ENDPOINT"],
                         [{"utilityId": "uta", "dcrImmutableId": os.environ["SWU_DCR_IMMUTABLE_ID"]}],
                         credential=credential)
    while True:
        try:
            for message in queue.receive_messages(messages_per_page=16, visibility_timeout=120):
                batches = json.loads(message.content)
                if set(batches) != {"SimAudit_CL"}:
                    raise ValueError("Unexpected lab audit stream")
                sink.send("uta", batches)
                queue.delete_message(message)
        except Exception:
            logging.exception("Lab audit relay failed; queue message retained")
        time.sleep(5)


if __name__ == "__main__":
    main()
