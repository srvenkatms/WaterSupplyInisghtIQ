import runpy
from pathlib import Path

import pytest

DEPLOY = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "deploy-route-ab.py"))


def route(identity):
    return {"id": identity, "directPlcAccess": identity == "routea", "machines": [
        {"name": name, "ip": ip} for name, ip in (
            ("vm-uta-plc", "10.30.1.10"), ("vm-uta-hmi", "10.30.1.11"),
            ("vm-uta-attack", "10.30.4.10"))]}


@pytest.mark.parametrize("identity", ["routea", "routeb"])
@pytest.mark.parametrize("role", ["vm-uta-plc", "vm-uta-attack"])
def test_control_repair_is_offline_scoped_and_preserves_private_credentials(identity, role):
    script = DEPLOY["control_repair_script"](route(identity), {"name": role}, {"storageAccountName": "teststorage"})
    assert "docker build --network none --pull=false" in script
    assert "FROM swu-apps" not in script  # Dockerfile payload is encoded.
    assert "docker rename" in script and "trap " in script
    assert "sha256sum /app/swuapps/route_worker.py" in script
    assert "sha256sum /app/swuapps/modbus_proxy.py" in script
    assert script.endswith("echo __SWU_RUNTIME_OK__\n")
    assert "swu-plc " not in script and "swu-plant " not in script
    assert "pip install" not in script and "apt-get" not in script
    assert "hmi.key" not in script and "hmi.json" not in script and "hmi.env" not in script
    if role == "vm-uta-attack":
        assert script.count("WHERE finished=0") == 2
        assert script.index("docker stop") < script.rindex("WHERE finished=0")
        assert "-v /opt/swu/private:/private:ro" in script
    else:
        writers = "SWU_MODBUS_WRITERS=10.30.1.10,10.30.1.11"
        assert writers + (",10.30.4.10 " if identity == "routea" else " ") in script


def test_control_repair_refuses_other_vm_roles():
    with pytest.raises(ValueError):
        DEPLOY["control_repair_script"](route("routea"), {"name": "vm-uta-gw"}, {})


@pytest.mark.parametrize("identity", ["routea", "routeb"])
@pytest.mark.parametrize("role", ["vm-uta-hmi", "vm-uta-attack"])
def test_protocol_publication_is_offline_and_preserves_existing_hmi_authentication(identity, role):
    script = DEPLOY["control_repair_script"](route(identity), {"name": role},
                                            {"storageAccountName": "teststorage"}, protocol_trace=True)
    assert "docker build --network none --pull=false" in script
    assert "sha256sum /app/swuapps/protocol_trace.py" in script
    assert "sha256sum /app/swuapps/hmi.py" in script
    assert "docker rename" in script and "trap " in script
    assert "pip install" not in script and "apt-get" not in script
    assert script.endswith("echo __SWU_RUNTIME_OK__\n")
    if role == "vm-uta-hmi" and identity == "routeb":
        assert "--env-file /opt/swu/private/hmi.env" in script
        assert "--certfile /private/hmi.crt --keyfile /private/hmi.key" in script
        assert "SWU_HMI_PASSWORD_HASH=" not in script and "SWU_HMI_SESSION_KEY=" not in script
    assert "swu-plc " not in script and "swu-proxy " not in script
