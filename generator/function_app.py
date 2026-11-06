"""Azure Functions app (Python v2 model) that runs the generator in Azure.

- generate: every minute, advances every utility and uploads its records.
- scenario: receives scenario requests from the test harness's queue.
State lives in blob storage, one blob per utility; scenario requests are
written as their own blobs so the two functions never write the same blob."""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceExistsError

from swugen import engine, load_profiles
from swugen.sink import IngestionSink
from swugen.account_state import AccountStateSink
from swugen.detection_history import DetectionHistorySink
from swugen import delivery
from swugen.requests import validate_request

app = func.FunctionApp()
CONTAINER = "generator-state"


def _blobs():
    account = os.environ["AzureWebJobsStorage__accountName"]
    svc = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=DefaultAzureCredential())
    c = svc.get_container_client(CONTAINER)
    if not c.exists():
        try:
            c.create_container()
        except ResourceExistsError:
            pass
    return c


@contextmanager
def leased_state(blob, initial):
    try:
        blob.upload_blob(json.dumps(initial), overwrite=False)
    except ResourceExistsError:
        pass
    lease = blob.acquire_lease(lease_duration=60)
    stopped = threading.Event()
    lease_error = []

    def renew():
        while not stopped.wait(20):
            try:
                lease.renew()
            except Exception as exc:
                logging.exception("State lease renewal failed")
                lease_error.append(exc)
                return

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()

    def save(state):
        if lease_error:
            raise RuntimeError("Lost generator state lease") from lease_error[0]
        blob.upload_blob(json.dumps(state), overwrite=True, lease=lease)

    try:
        yield json.loads(blob.download_blob(lease=lease).readall()), save
    finally:
        stopped.set()
        thread.join()
        lease.release()


@app.timer_trigger(schedule="0 * * * * *", arg_name="timer", run_on_startup=False)
def generate(timer: func.TimerRequest) -> None:
    now = engine.utcnow()
    try:
        accel = int(os.environ.get("SWU_TIME_ACCELERATION", "1"))
        if not 1 <= accel <= 60:
            raise ValueError("Time acceleration must be between 1 and 60")
        container, sink = _blobs(), IngestionSink.from_env()
        profiles = load_profiles()
        configured = {u["utilityId"] for u in json.loads(os.environ["SWU_UTILITIES"])}
        accounts = AccountStateSink(json.loads(os.environ["SWU_UTILITIES"]), DefaultAzureCredential())
    except Exception:
        logging.exception("swu-generate failed during initialization")
        raise
    errors = []
    for uid, p in profiles.items():
        if uid not in configured:
            continue
        try:
            generate_utility(uid, p, profiles, container, sink, accounts, now, accel)
        except Exception as exc:
            logging.exception("swu-generate failed for %s", uid)
            errors.append(exc)
    if errors:
        raise ExceptionGroup("One or more utility generators failed", errors)


def generate_utility(uid, profile, profiles, container, sink, accounts, now, accel):
    state_blob = container.get_blob_client(f"state/{uid}.json")
    with leased_state(state_blob, engine.new_state(profile, now)) as (state, save):
        delivery.flush(uid, state, sink, save, accounts)
        for item in container.list_blobs(name_starts_with=f"requests/{uid}/"):
            try:
                req = json.loads(container.download_blob(item.name).readall())
                validate_request(req, profiles)
                if req["utility"] != uid:
                    raise ValueError("Cross-utility scenario request")
                engine.add_scenario(state, req["scenario"], req["runId"], datetime.fromisoformat(req["start"]))
            except (ValueError, KeyError, TypeError) as exc:
                logging.error("Rejected request %s for %s: %s", item.name, uid, exc)
                state.setdefault("rejectedRuns", {})[item.name] = str(exc)
                save(state)
                container.delete_blob(item.name)
                continue
            save(state)
            container.delete_blob(item.name)
            logging.info("Started %s for %s (run %s)", req["scenario"], uid, req["runId"])
        delivery.prepare(profile, state, now, accel)
        save(state)
        counts = delivery.flush(uid, state, sink, save, accounts)
        logging.info("swu-generate utility=%s counts=%s", uid, counts)


@app.timer_trigger(schedule="30 * * * * *", arg_name="timer", run_on_startup=False)
def detection_history(timer: func.TimerRequest) -> None:
    try:
        utilities = json.loads(os.environ["SWU_UTILITIES"])
        container = _blobs()
        history = DetectionHistorySink(utilities, DefaultAzureCredential())
    except Exception:
        logging.exception("swu-history failed during initialization")
        raise
    errors = []
    for utility in utilities:
        uid = utility["utilityId"]
        try:
            blob = container.get_blob_client(f"history/{uid}.json")
            with leased_state(blob, {}) as (state, save):
                updated = history.refresh(uid, state, save)
            logging.info("swu-history utility=%s updated=%s", uid, updated)
        except Exception as exc:
            logging.exception("swu-history failed for %s", uid)
            errors.append(exc)
    if errors:
        raise ExceptionGroup("One or more detection history refreshes failed", errors)


@app.queue_trigger(arg_name="msg", queue_name="scenarios", connection="AzureWebJobsStorage")
def scenario(msg: func.QueueMessage) -> None:
    req = json.loads(msg.get_body().decode())
    profiles = load_profiles()
    if not isinstance(req, dict):
        raise ValueError("Scenario request must be an object")
    req.setdefault("start", datetime.now(timezone.utc).isoformat())
    validate_request(req, profiles)
    _blobs().upload_blob(f"requests/{req['utility']}/{req['runId']}.json", json.dumps(req), overwrite=True)
