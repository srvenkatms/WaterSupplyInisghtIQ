"""Publish the private Route A/B runtime using only its separate CPI manifest."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "route-ab-deployment.json"
REVISION = "b5d41356dab4aeadca0dd7ca64ba542f870b595d"


def source_package() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for folder, target in [("apps/swuapps", "swuapps"), ("apps/openplc", "openplc"),
                               ("generator/swugen", "swugen"), ("profiles", "profiles")]:
            for path in sorted(ROOT.joinpath(*folder.split("/")).rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    archive.add(path, arcname=f"{target}/{path.relative_to(ROOT.joinpath(*folder.split('/'))).as_posix()}")
        for name in ("Dockerfile", "requirements.txt"):
            archive.add(ROOT / "apps" / name, arcname=name)
    return output.getvalue()


def tls_material(host: str) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SWU Route B private HMI")])
    stamp = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(stamp - timedelta(minutes=5))
            .not_valid_after(stamp + timedelta(days=180))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


def private_file(path: str, content: str) -> str:
    encoded = base64.b64encode(content.encode()).decode()
    return f"printf '%s' '{encoded}' | base64 -d > {path}\nchmod 600 {path}\n"


def start(name: str, arguments: str) -> str:
    return (f"if docker container inspect {name} >/dev/null 2>&1; then docker rm -f {name} >/dev/null; fi\n"
            f"docker run -d --name {name} --restart unless-stopped {arguments} >/dev/null\n")


def application_script(route: dict, machine: dict, state: dict, credentials: dict) -> str:
    ip = {vm["name"]: vm["ip"] for vm in route["machines"]}
    plc, hmi = ip["vm-uta-plc"], ip["vm-uta-hmi"]
    role = machine["name"]
    script = "set -eu\numask 077\ninstall -d -m 700 /opt/swu/private /opt/swu/state /opt/swu/gateway /opt/swu/sensors\n"
    if role == "vm-uta-plc":
        writers = f"{plc},{hmi}" + (f",{ip['vm-uta-attack']}" if route["directPlcAccess"] else "")
        script += start("swu-plant", "--network host -v /opt/swu/sensors:/sensors "
                        "-e SWU_SENSOR_FILE=/sensors/inputs.bin -e SWU_PLC_HOST=127.0.0.1 "
                        "-e SWU_MODBUS_PORT=1502 swu-apps python -m swuapps.plant")
        script += start("swu-plc", "-p 127.0.0.1:1502:1502 -v /opt/swu/sensors:/sensors:ro "
                        "-v /opt/swu/src/profiles/uta.json:/config/profile.json:ro swu-openplc")
        script += start("swu-proxy", f"--network host -e SWU_MODBUS_WRITERS={writers} swu-apps python -m swuapps.modbus_proxy")
        script += "for i in $(seq 1 90); do\n[ \"$(docker inspect -f '{{.State.Health.Status}}' swu-plc)\" = healthy ] && break\nsleep 2\ndone\n"
        script += "[ \"$(docker inspect -f '{{.State.Health.Status}}' swu-plc)\" = healthy ]\n"
        script += f"[ \"$(docker exec swu-plc git -C /opt/openplc rev-parse HEAD)\" = {REVISION} ]\n"
    elif role == "vm-uta-hmi":
        arguments = f"--network host -e SWU_PLC_HOST={plc} "
        if route["id"] == "routeb":
            from werkzeug.security import generate_password_hash
            env = (f"SWU_PLC_HOST={plc}\nSWU_HMI_AUTH=password\nSWU_HMI_USERNAME={credentials['username']}\n"
                   f"SWU_HMI_PASSWORD_HASH={generate_password_hash(credentials['password'])}\n"
                   f"SWU_HMI_SESSION_KEY={secrets.token_urlsafe(48)}\n")
            script += private_file("/opt/swu/private/hmi.env", env)
            script += private_file("/opt/swu/private/hmi.crt", credentials["certificate"])
            script += private_file("/opt/swu/private/hmi.key", credentials["key"])
            arguments = "--network host --env-file /opt/swu/private/hmi.env -v /opt/swu/private:/private:ro "
        command = "gunicorn --bind 0.0.0.0:1881 --workers 1 --threads 4 "
        if route["id"] == "routeb":
            command += "--certfile /private/hmi.crt --keyfile /private/hmi.key "
        script += start("swu-hmi", arguments + "swu-apps " + command + "swuapps.hmi:app")
    elif role == "vm-uta-attack":
        if route["id"] == "routeb":
            script += private_file("/opt/swu/private/hmi.json", json.dumps({
                key: credentials[key] for key in ("username", "password")}))
            script += private_file("/opt/swu/private/hmi.crt", credentials["certificate"])
        script += start("swu-runner", f"--network host -v /opt/swu/state:/state -v /opt/swu/private:/private:ro "
                        f"-e SWU_ROUTE={route['id']} -e SWU_STORAGE_ACCOUNT={state['storageAccountName']} "
                        f"-e SWU_PLC_HOST={plc} -e SWU_HMI_URL=https://{hmi}:1881 "
                        "swu-apps python -m swuapps.route_worker")
    elif role == "vm-uta-gw":
        environment = (f"-e SWU_PLC_HOST={plc} -e SWU_DCE_ENDPOINT={state['dceEndpoint']} "
                       f"-e SWU_DCR_IMMUTABLE_ID={route['dcrImmutableId']} "
                       f"-e SWU_ROUTE={route['id']} -e SWU_STORAGE_ACCOUNT={state['storageAccountName']} ")
        script += start("swu-gateway", "--network host -v /opt/swu/gateway:/state -e SWU_STATE_DIR=/state "
                        + environment + "swu-apps python -m swuapps.gateway")
        script += start("swu-audit-relay", "--network host " + environment + "swu-apps python -m swuapps.audit_relay")
    else:
        raise ValueError("This publisher does not replace the operator VM")
    return script + "echo __SWU_RUNTIME_OK__\n"


def control_repair_script(route: dict, machine: dict, state: dict, *, protocol_trace: bool = False) -> str:
    role = machine["name"]
    allowed = ("vm-uta-hmi", "vm-uta-attack") if protocol_trace else ("vm-uta-plc", "vm-uta-attack")
    if role not in allowed:
        raise ValueError("Offline publication only replaces the selected application roles")
    modules = (("hmi.py", "route_worker.py", "protocol_trace.py") if protocol_trace else
               ("modbus_proxy.py", "route_worker.py", "protocol_trace.py"))
    sources = {name: (ROOT / "apps" / "swuapps" / name).read_bytes() for name in modules}
    digest = hashlib.sha256(b"".join(sources.values())).hexdigest()
    image = f"swu-apps-control:{digest[:16]}"
    script = "set -eu\numask 077\ninstall -d /opt/swu/src/swuapps\n"
    for name, content in sources.items():
        script += private_file(f"/opt/swu/src/swuapps/{name}", content.decode())
    script += private_file("/opt/swu/control-repair.Dockerfile",
                          "FROM swu-apps\nCOPY " + " ".join(f"swuapps/{name}" for name in modules) + " /app/swuapps/\n")
    script += (f"docker build --network none --pull=false -q -t {image} "
               "-f /opt/swu/control-repair.Dockerfile /opt/swu/src\n")
    ips = {vm["name"]: vm["ip"] for vm in route["machines"]}
    if role == "vm-uta-attack":
        name = "swu-runner"
        # Check again after a graceful stop so a concurrent queued action cannot be discarded.
        check = ("python -c \"import sqlite3;d=sqlite3.connect('file:/state/commands.sqlite?mode=ro',uri=True);"
                 "assert d.execute('SELECT count(*) FROM jobs WHERE finished=0').fetchone()[0]==0,"
                 "'An unfinished action must restore before control repair'\"")
        script += f"docker exec {name} {check}\n"
        arguments = (f"--network host -v /opt/swu/state:/state -v /opt/swu/private:/private:ro "
                     f"-e SWU_ROUTE={route['id']} -e SWU_STORAGE_ACCOUNT={state['storageAccountName']} "
                     f"-e SWU_PLC_HOST={ips['vm-uta-plc']} -e SWU_HMI_URL=https://{ips['vm-uta-hmi']}:1881 "
                     f"{image} python -m swuapps.route_worker")
    elif role == "vm-uta-hmi":
        name = "swu-hmi"
        environment = f"--network host -e SWU_PLC_HOST={ips['vm-uta-plc']} "
        tls = ""
        if route["id"] == "routeb":
            environment = "--network host --env-file /opt/swu/private/hmi.env -v /opt/swu/private:/private:ro "
            tls = "--certfile /private/hmi.crt --keyfile /private/hmi.key "
        arguments = (f"{environment}{image} gunicorn --bind 0.0.0.0:1881 --workers 1 --threads 4 "
                     f"{tls}swuapps.hmi:app")
    else:
        name = "swu-proxy"
        writers = f"{ips['vm-uta-plc']},{ips['vm-uta-hmi']}"
        if route["directPlcAccess"]:
            writers += f",{ips['vm-uta-attack']}"
        arguments = (f"--network host -e SWU_MODBUS_WRITERS={writers} "
                     f"{image} python -m swuapps.modbus_proxy")
    backup = name + "-before-control-repair"
    script += f"if docker container inspect {backup} >/dev/null 2>&1; then echo 'Repair rollback container exists; inspect before retrying' >&2; exit 1; fi\n"
    script += f"docker stop --time 30 {name} >/dev/null\n"
    script += f"trap 'docker start {name} >/dev/null' EXIT\n"
    if role == "vm-uta-attack":
        script += (f"docker run --rm --network none -v /opt/swu/state:/state:ro "
                   f"--entrypoint python {image} -c "
                   "\"import sqlite3;d=sqlite3.connect('file:/state/commands.sqlite?mode=ro',uri=True);"
                   "assert d.execute('SELECT count(*) FROM jobs WHERE finished=0').fetchone()[0]==0,"
                   "'Action still unfinished; old runner will resume recovery'\"\n")
    script += f"docker rename {name} {backup}\n"
    script += (f"trap 'docker rm -f {name} >/dev/null 2>&1 || :; "
               f"docker rename {backup} {name}; docker start {name} >/dev/null' EXIT\n")
    script += f"docker run -d --name {name} --restart unless-stopped {arguments} >/dev/null\n"
    script += "sleep 3\n"
    script += f"[ \"$(docker inspect -f '{{{{.State.Running}}}}' {name})\" = true ]\n"
    for module, content in sources.items():
        script += (f"[ \"$(docker exec {name} sha256sum /app/swuapps/{module} | cut -d ' ' -f1)\" "
                   f"= {hashlib.sha256(content).hexdigest()} ]\n")
    script += f"trap - EXIT\ndocker rm {backup} >/dev/null\necho __SWU_RUNTIME_OK__\n"
    return script


class Publisher:
    def __init__(self, state: dict, config: Path, owner: str):
        self.state = state
        self.environment = os.environ | {"AZURE_CONFIG_DIR": str(config.resolve())}
        self.owner = owner
        self._arm_token = None
        self._arm_expiry = 0
        self._token_lock = threading.Lock()

    def azure(self, *args: str, timeout=3600):
        result = subprocess.run([shutil.which("az"), *args, "--only-show-errors", "-o", "json"],
                                env=self.environment, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"Azure {args[0]} failed: {result.stderr[-5000:]}")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def context(self):
        spec = importlib.util.spec_from_file_location("route_foundation", ROOT / "scripts" / "provision-route-ab.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.check_context(self.azure("account", "show"), self.azure("ad", "signed-in-user", "show"),
                             self.state["subscriptionId"], self.state["tenantId"], self.owner)
        module.check_groups(self.azure("group", "list"), self.state["prefix"])
        if self.state.get("phase") != "verified":
            raise RuntimeError("Start from a verified foundation manifest")

    def persist(self):
        temporary = STATE.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2) + "\n")
        temporary.replace(STATE)

    def arm(self, url: str, body: dict | None = None):
        with self._token_lock:
            if time.time() >= self._arm_expiry - 300:
                token = self.azure("account", "get-access-token", "--resource", "https://management.azure.com/", timeout=180)
                self._arm_token, self._arm_expiry = token["accessToken"], int(token["expires_on"])
            bearer = self._arm_token
        for attempt in range(5):
            request = Request(url, method="PUT" if body is not None else "GET",
                              data=json.dumps(body).encode() if body is not None else None,
                              headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=120) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 4:
                    raise RuntimeError(f"Azure ARM operation failed ({error.code}); protected request contents are not logged") from error
                print(f"Azure ARM temporary failure ({error.code}); retrying the same operation.", flush=True)
                time.sleep(min(60, 5 * 2 ** attempt))
            except (URLError, TimeoutError, ConnectionError) as error:
                if attempt == 4:
                    raise RuntimeError("Azure ARM transport failed after bounded retries; no success is asserted") from error
                print("Azure ARM transport interrupted; retrying the same operation.", flush=True)
                time.sleep(min(60, 5 * 2 ** attempt))

    def foundation(self, key: Path, outbound: bool):
        user = self.azure("ad", "signed-in-user", "show")
        values = {key: {"value": value} for key, value in {
            "location": self.state["location"], "prefix": self.state["prefix"], "owner": self.owner,
            "vmSize": self.state["vmSize"], "adminSshPublicKey": key.read_text().strip(),
            "testerPrincipalId": user["id"], "deployedAt": self.state["deployedAt"],
            "allowBuildOutbound": outbound, "deployAnalyticsRules": True, "enableRouteRuntime": True,
            "preserveConsoleSubnet": (ROOT / "route-ab-console-deployment.json").exists(),
        }.items()}
        with tempfile.TemporaryDirectory(prefix="swu-runtime-") as directory:
            parameters = Path(directory) / "parameters.json"
            parameters.write_text(json.dumps({"parameters": values}))
            compiler = Path(self.environment["AZURE_CONFIG_DIR"]) / "bin" / ("bicep.exe" if os.name == "nt" else "bicep")
            template = Path(directory) / "template.json"
            compiled = subprocess.run([str(compiler), "build", str(ROOT / "infra" / "route-ab.bicep"),
                                       "--outfile", str(template)], capture_output=True, text=True, timeout=120)
            if compiled.returncode:
                raise RuntimeError(f"Installed Bicep compilation failed: {compiled.stderr}")
            args = ("--subscription", self.state["subscriptionId"], "--location", self.state["location"],
                    "--template-file", str(template), "--parameters", f"@{parameters}")
            preview = self.azure("deployment", "sub", "what-if", "--name", "swu-route-runtime", *args,
                                 "--no-pretty-print")
            if preview.get("status") != "Succeeded" or any(
                    change.get("changeType") == "Delete" for change in preview.get("changes", [])):
                raise RuntimeError("Runtime infrastructure preview failed or would delete a resource")
            result = self.azure("deployment", "sub", "create", "--name", "swu-route-runtime", *args)
            if result["properties"]["provisioningState"] != "Succeeded":
                raise RuntimeError("Runtime infrastructure deployment failed")
        self.state.update({name: value["value"] for name, value in result["properties"]["outputs"].items()})
        self.persist()

    def extension(self, machine: dict, script: str):
        # Protected settings keep bootstrap credentials out of ARM readback, files and CLI arguments.
        encoded = base64.b64encode(gzip.compress(script.encode())).decode()
        if len(encoded) > 256000:
            raise ValueError("Protected runtime bootstrap exceeds the extension limit")
        body = {"location": self.state["location"], "properties": {
            "publisher": "Microsoft.Azure.Extensions", "type": "CustomScript", "typeHandlerVersion": "2.1",
            "autoUpgradeMinorVersion": True, "forceUpdateTag": str(uuid.uuid4()),
            "protectedSettings": {"script": encoded}}}
        url = f"https://management.azure.com{machine['id']}/extensions/swu-runtime?api-version=2024-07-01"
        self.arm(url, body)
        deadline = time.monotonic() + 3300
        while time.monotonic() < deadline:
            result = self.arm(url + "&$expand=instanceView")
            status = result["properties"].get("provisioningState")
            if status == "Succeeded":
                view = result["properties"].get("instanceView", {})
                output = "\n".join(value.get("message", "") for value in
                                   view.get("statuses", []) + view.get("substatuses", []))
                marker = "__SWU_BUILD_OK__" if script.endswith("echo __SWU_BUILD_OK__\n") else "__SWU_RUNTIME_OK__"
                if marker not in output:
                    raise RuntimeError(f"{machine['id']}: guest success marker was not present")
                print(f"Runtime operation completed: {machine['id'].split('/resourceGroups/')[1]}", flush=True)
                return
            if status in ("Failed", "Canceled"):
                raise RuntimeError(f"{machine['id']}: protected runtime bootstrap failed; inspect extension instance view")
            time.sleep(15)
        raise TimeoutError(f"{machine['id']}: runtime bootstrap exceeded 55 minutes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--azure-config-dir", type=Path, required=True)
    parser.add_argument("--ssh-public-key", type=Path)
    parser.add_argument("--owner", required=True)
    updates = parser.add_mutually_exclusive_group()
    updates.add_argument("--repair-control-path", action="store_true",
                        help="Offline image update of existing proxies/runners only; preserve credentials and network policy")
    updates.add_argument("--publish-protocol-trace", action="store_true",
                         help="Offline image update of existing HMI/runners; preserve private HMI credentials and TLS")
    args = parser.parse_args()
    publisher = Publisher(json.loads(STATE.read_text()), args.azure_config_dir, args.owner)
    publisher.context()
    if args.repair_control_path or args.publish_protocol_trace:
        if not publisher.state.get("runtimeDeployed"):
            raise RuntimeError("Control repair requires the existing deployed runtime")
        roles = ("vm-uta-hmi", "vm-uta-attack") if args.publish_protocol_trace else ("vm-uta-attack", "vm-uta-plc")
        for role in roles:
            tasks = [(route, machine) for route in publisher.state["routes"]
                     for machine in route["machines"] if machine["name"] == role]
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda task: publisher.extension(
                    task[1], control_repair_script(task[0], task[1], publisher.state,
                                                  protocol_trace=args.publish_protocol_trace)), tasks))
        modules = (("hmi.py", "route_worker.py", "protocol_trace.py") if args.publish_protocol_trace else
                   ("route_worker.py", "modbus_proxy.py", "protocol_trace.py"))
        publisher.state["protocolTracePublication" if args.publish_protocol_trace else "controlRepair"] = {
            "publishedAt": datetime.now(timezone.utc).isoformat(),
            "sourceSha256": {name: hashlib.sha256((ROOT / "apps" / "swuapps" / name).read_bytes()).hexdigest()
                             for name in modules},
            "state": "validation-required",
        }
        publisher.persist()
        print("Offline application update deployed; live protocol/readback/restoration verification is still required.", flush=True)
        return
    if args.ssh_public_key is None:
        parser.error("--ssh-public-key is required for a full runtime publication")
    package = source_package()
    digest = hashlib.sha256(package).hexdigest()
    credentials = {"username": "operator@uta.lab.example", "password": secrets.token_urlsafe(32)}
    credentials["certificate"], credentials["key"] = tls_material("10.40.1.11")
    publisher.state["runtimeDeployed"] = False
    publisher.state["runtimePhase"] = "building"
    publisher.persist()
    try:
        print("Opening the existing, controlled package-build egress window.", flush=True)
        publisher.foundation(args.ssh_public_key, True)
        tasks = [(route, machine) for route in publisher.state["routes"]
                 for machine in route["machines"] if machine["name"] != "vm-uta-operator"]
        source = base64.b64encode(package).decode()

        def build(task):
            route, machine = task
            script = ("set -eu\numask 077\ninstall -d /opt/swu/src/wheelhouse\n"
                      f"printf '%s' '{source}' | base64 -d | tar -xzf - -C /opt/swu/src\n"
                      "docker build -q -t swu-apps /opt/swu/src > /opt/swu/build-apps.log 2>&1\n")
            if machine["name"] == "vm-uta-plc":
                script += "docker build -q -t swu-openplc /opt/swu/src/openplc > /opt/swu/build-openplc.log 2>&1\n"
            publisher.extension(machine, script + "echo __SWU_BUILD_OK__\n")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(build, tasks))
        publisher.state.update(runtimePhase="configuring", runtimeSourceSha256=digest)
        publisher.persist()
        # Establish the controller/HMI before the workers start reading.
        for role in ("vm-uta-plc", "vm-uta-hmi", "vm-uta-gw", "vm-uta-attack"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda task: publisher.extension(
                    task[1], application_script(task[0], task[1], publisher.state, credentials)),
                    [task for task in tasks if task[1]["name"] == role]))
        publisher.state["runtimePhase"] = "validation-required"
        publisher.persist()
    except Exception:
        publisher.state["runtimePhase"] = "deployment-failed"
        publisher.persist()
        raise
    finally:
        credentials.clear()
        print("Closing package-build egress for both routes, including failure recovery.", flush=True)
        publisher.foundation(args.ssh_public_key, False)
    print("Both private application stacks are deployed. Live route/Sentinel validation is still required.", flush=True)


if __name__ == "__main__":
    main()
