"""Minimal web HMI (Purdue level 2): live plant values and setpoint changes.
Stands in for an open-source HMI such as FUXA; any HMI that speaks Modbus TCP
to the same register map can replace it."""
from __future__ import annotations

import os
import secrets
from datetime import timedelta

from flask import Flask, redirect, request, jsonify, abort, session
import logging
from pymodbus.client import ModbusTcpClient
from werkzeug.security import check_password_hash

from .registers import MAP, READ_COUNT, READ_START, SETPOINTS, encode, from_block

app = Flask(__name__)
HOST, PORT = os.environ.get("SWU_PLC_HOST", "127.0.0.1"), int(os.environ.get("SWU_MODBUS_PORT", "502"))
AUTH = os.environ.get("SWU_HMI_AUTH", "none")
if AUTH not in ("none", "password"):
    raise ValueError("SWU_HMI_AUTH must be none or password")
if AUTH == "password":
    if not os.environ.get("SWU_HMI_USERNAME") or not os.environ.get("SWU_HMI_PASSWORD_HASH"):
        raise ValueError("Password HMI requires a username and password hash")
    app.secret_key = os.environ["SWU_HMI_SESSION_KEY"]
    if len(app.secret_key) < 32:
        raise ValueError("HMI session key must contain at least 32 characters")
    app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict", PERMANENT_SESSION_LIFETIME=timedelta(minutes=15))


@app.before_request
def authorize():
    if AUTH != "password":
        return
    if not request.is_secure:
        abort(403, "Password HMI requires HTTPS")
    if request.endpoint not in ("login", "session_info") and not session.get("authenticated"):
        abort(401, "HMI password login is required")
    if request.method == "POST":
        if request.headers.get("Origin", request.host_url.rstrip("/")) != request.host_url.rstrip("/"):
            abort(403, "Cross-origin changes are not allowed")
        supplied = request.headers.get("X-SWU-CSRF", "") or request.form.get("csrf", "")
        if not session.get("csrf") or not secrets.compare_digest(supplied, session["csrf"]):
            abort(403, "A session CSRF token is required")


@app.get("/api/session")
def session_info():
    if AUTH == "password":
        session.setdefault("csrf", secrets.token_urlsafe(32))
    return jsonify(authentication=AUTH, authenticated=bool(session.get("authenticated")),
                   csrf=session.get("csrf"), mfa=False)


@app.post("/api/login")
def login():
    if AUTH != "password":
        abort(404)
    body = request.get_json()
    if not isinstance(body, dict) or set(body) != {"username", "password"}:
        abort(400, "Provide username and password")
    if not all(isinstance(value, str) and len(value) <= 256 for value in body.values()):
        abort(400, "Invalid login fields")
    valid_password = check_password_hash(os.environ["SWU_HMI_PASSWORD_HASH"], body["password"])
    if body["username"] != os.environ["SWU_HMI_USERNAME"] or not valid_password:
        app.logger.warning("Rejected HMI password login from %s", request.remote_addr)
        abort(401, "Invalid HMI credentials")
    session.clear()
    session.update(authenticated=True, csrf=secrets.token_urlsafe(32))
    session.permanent = True
    app.logger.info("HMI password login succeeded from %s; MFA is not enabled", request.remote_addr)
    return jsonify(authenticated=True, csrf=session["csrf"], mfa=False)


def _client():
    c = ModbusTcpClient(HOST, port=PORT)
    if not c.connect():
        raise ConnectionError("HMI cannot connect to PLC")
    return c


@app.errorhandler(ConnectionError)
def unavailable(error):
    logging.exception("HMI PLC operation failed")
    return jsonify(error=str(error)), 503


@app.get("/api/values")
def values():
    c = _client()
    try:
        response = c.read_holding_registers(READ_START, count=READ_COUNT)
        if response.isError():
            raise ConnectionError(f"PLC read failed: {response}")
        return jsonify(from_block(response.registers))
    finally:
        c.close()


@app.get("/")
def index():
    v = values().get_json()
    rows = "".join(f"<tr><td>{k}</td><td>{v[k]:.2f}</td></tr>" for k in ("TankLevelPct", "ClResidual", "PumpState", "DoseRate"))
    forms = "".join(f"<form method=post action=/set><input type=hidden name=csrf value='{session.get('csrf', '')}'><label>{k} <input name=value value='{v[k]:.2f}'></label>"
                    f"<input type=hidden name=tag value={k}><button>Set</button></form>" for k in SETPOINTS)
    return (f"<html><head><meta http-equiv=refresh content=5><title>Utility A HMI</title></head><body>"
            f"<h1>Utility A plant</h1><table>{rows}</table><h2>Setpoints</h2>{forms}</body></html>")


@app.post("/set")
@app.post("/api/setpoint")
def set_value():
    body = request.get_json() if request.is_json else request.form
    if not body:
        abort(400, "Provide a tag and value")
    tag = body.get("tag", "")
    try:
        value = float(body["value"])
        encoded = encode(value)
    except (KeyError, ValueError, TypeError):
        logging.warning("Rejected invalid HMI setpoint input")
        abort(400, "Invalid setpoint value")
    if tag not in SETPOINTS:
        logging.warning("Rejected HMI write to non-setpoint tag %s", tag)
        abort(400, "Tag is not writable")
    if tag in ("PumpOverride", "Mode") and value not in (0, 1):
        abort(400, "State values must be 0 or 1")
    if AUTH == "password" and (
        (tag in ("PumpLowSetpoint", "PumpHighSetpoint") and not 5 <= value <= 95)
        or (tag == "ClDoseSetpoint" and not 0 <= value <= 5)
    ):
        abort(400, "Setpoint exceeds the lab control bounds")
    c = _client()
    try:
        response = c.write_register(MAP[tag], encoded)
        if response.isError():
            raise ConnectionError(f"PLC rejected HMI setpoint write: {response}")
    finally:
        c.close()
    app.logger.info("HMI setpoint write: tag=%s value=%s source=%s", tag, value, request.remote_addr)
    return jsonify(tag=tag, value=value, written=True) if request.is_json else redirect("/")


def main() -> None:
    tls = (os.environ["SWU_HMI_TLS_CERT"], os.environ["SWU_HMI_TLS_KEY"]) if AUTH == "password" else None
    app.run(host="0.0.0.0", port=int(os.environ.get("SWU_HMI_PORT", "1881")), ssl_context=tls)


if __name__ == "__main__":
    main()
