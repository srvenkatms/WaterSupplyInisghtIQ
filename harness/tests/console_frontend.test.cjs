const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const directory = path.resolve(__dirname, "../../apps/swuapps/console_static");
const source = readFileSync(path.join(directory, "console.js"), "utf8");
const apiSource = source.slice(0, source.indexOf("\nfunction formValue()"));
const liveSource = readFileSync(path.join(directory, "live.js"), "utf8");

function response(status, body, type = "basic") {
  return {status, type, ok: status >= 200 && status < 300,
    headers: {get: () => "application/json"}, json: async () => body};
}

function element() {
  const classes = new Set();
  return {textContent: "", hidden: true, disabled: true, attributes: {}, listeners: {},
    classList: {add: value => classes.add(value), contains: value => classes.has(value),
      toggle: (value, enabled) => enabled ? classes.add(value) : classes.delete(value)},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    addEventListener(name, callback) { this.listeners[name] = callback; }};
}

function page(fetch) {
  const nodes = new Map([["message", element()], ["session-expired", element()]]);
  const actions = {};
  const evidence = {};
  for (const route of ["routea", "routeb"]) {
    for (const name of ["card", "health", "reading-state", "plant", "machinery-state", "water", "level", "chlorine",
      "pump", "dose", "tank-trend", "chlorine-trend", "job", "evidence", "plc-state",
      "sensor-level", "sensor-chlorine", "command-state", "plc-command", "trace-panel", "trace-state", "protocol-trace"]) {
      nodes.set(`${route}-${name}`, element());
    }
    actions[route] = ["pump-on", "chlorine-up"].map(action =>
      Object.assign(element(), {dataset: {route, action}}));
    evidence[route] = element();
  }
  nodes.set("routeb-hmi-state", element());
  nodes.set("routeb-hmi-pump", element());
  const timers = [];
  const logs = [];
  const context = vm.createContext({
    fetch, console: {error: (...args) => logs.push(args)},
    AbortSignal: {timeout: () => ({})},
    document: {
      body: {dataset: {hosting: "azure"}},
      getElementById: id => nodes.get(id) || null,
      querySelector(selector) {
        if (selector === 'meta[name="swu-csrf"]') return {content: "unit-csrf"};
        const match = selector.match(/^\[data-evidence="(routea|routeb)"\]$/);
        assert.ok(match, `Unexpected selector: ${selector}`);
        return evidence[match[1]];
      },
      querySelectorAll(selector) {
        if (selector === "[data-action]") return Object.values(actions).flat();
        if (selector === "[data-evidence]") return Object.values(evidence);
        const match = selector.match(/^\[data-route="(routea|routeb)"\]$/);
        assert.ok(match, `Unexpected selector: ${selector}`);
        return actions[match[1]];
      },
    },
    window: {setTimeout: callback => timers.push(callback), confirm: () => true},
  });
  vm.runInContext(apiSource, context);
  return {api: vm.runInContext("api", context), context, nodes, actions, timers, logs};
}

const drain = () => new Promise(resolve => setImmediate(resolve));

for (const route of ["routea", "routeb"]) {
  test(`${route} opens the raw trace on click and renders only reported bytes for that run`, async () => {
    let result = {runId: "wire-run", action: "pump-on", stage: "queued"};
    const view = page(async (url, options) => {
      if (url === "/api/live/routes") return response(200, routes());
      if (options.method === "POST") return response(202, result);
      return response(200, result);
    });
    vm.runInContext(liveSource, view.context);
    await drain();
    await view.actions[route][0].listeners.click();
    assert.equal(view.nodes.get(`${route}-trace-panel`).open, true);
    assert.ok(!view.nodes.get(`${route}-protocol-trace`).textContent.includes("00 64"));
    result = {...result, stage: "plc-observed", protocolTrace: {droppedEvents: 2, events: [
      {sequence: 1, at: "2026-11-06T00:00:00Z", phase: "override-write", protocol: "Modbus TCP",
        channel: route === "routea" ? "worker-to-plc" : "hmi-to-plc", direction: "TX",
        status: "socket-accepted", hex: "00 01 00 00 00 06 01 06 00 0D 00 64",
        frames: [{functionCode: 6, address: 13, rawValue: 100}]},
      {sequence: 2, at: "2026-11-06T00:00:01Z", phase: "override-write", protocol: "Modbus TCP",
        channel: route === "routea" ? "worker-to-plc" : "hmi-to-plc", direction: "RX",
        status: "received", hex: "00 01 00 00 00 06 01 06 00 0D 00 64", frames: []},
    ]}};
    view.timers.shift()();
    await drain();
    const raw = view.nodes.get(`${route}-protocol-trace`).textContent;
    assert.match(raw, /TX \/ Modbus TCP/);
    assert.match(raw, /RX \/ Modbus TCP/);
    assert.match(raw, /00 0D 00 64/);
    assert.match(raw, /"address":13,"rawValue":100/);
    assert.match(view.nodes.get(`${route}-trace-state`).textContent, /TRUNCATED: 2/);
    assert.ok(!view.nodes.get(`${route}-job`).textContent.includes("protocolTrace"));
    result = {runId: "older-no-trace", action: "pump-on", stage: "completed-restored"};
    view.timers.shift()();
    await drain();
    assert.ok(!view.nodes.get(`${route}-protocol-trace`).textContent.includes("00 0D 00 64"));
    assert.match(view.nodes.get(`${route}-trace-state`).textContent, /Older commands are not reconstructed/);
  });
}

test("API keeps successful JSON, CSRF and same-origin manual redirects", async () => {
  let supplied;
  const view = page(async (url, options) => {
    supplied = options;
    return response(200, {ready: true});
  });
  assert.equal((await view.api("/api/status")).ready, true);
  assert.equal(supplied.redirect, "manual");
  assert.equal(supplied.headers["X-SWU-CSRF"], "unit-csrf");
  assert.equal(view.nodes.get("session-expired").hidden, true);
});

for (const expired of [response(0, null, "opaqueredirect"), response(401, {})]) {
  test(`expired sign-in is explicit for ${expired.type}/${expired.status}`, async () => {
    let calls = 0;
    const view = page(async () => { calls++; return expired; });
    await assert.rejects(view.api("/api/live/routes"), error =>
      error.authenticationRequired === true && /Sign-in expired/.test(error.message));
    assert.equal(view.nodes.get("session-expired").hidden, false);
    assert.equal(calls, 1);
  });
}

test("a failed control request is not automatically replayed", async () => {
  let calls = 0;
  const view = page(async (url, options) => {
    calls++;
    assert.equal(options.method, "POST");
    assert.equal(options.redirect, "manual");
    throw new TypeError("Failed to fetch");
  });
  await assert.rejects(view.api("/api/live/commands", {method: "POST", body: "{}"}),
    /request outcome is unconfirmed/);
  assert.equal(calls, 1);
  assert.equal(view.nodes.get("session-expired").hidden, true);
  assert.equal(view.logs.length, 1);
});

test("server error details retain the uncertain run identifier", async () => {
  const view = page(async () => response(503, {error: "Dispatch uncertain", runId: "known-run"}));
  await assert.rejects(view.api("/api/live/commands", {method: "POST"}), error =>
    error.message === "Dispatch uncertain" && error.details.runId === "known-run");
});

function routes(stale = false, pump = 0, level = 80.9) {
  const at = new Date(Date.now() - (stale ? 3600000 : 0)).toISOString();
  return {routes: ["routea", "routeb"].map(route => ({
    route, ready: true, stale, observedAt: at, path: route,
    values: {TankLevelPct: level, ClResidual: 1.08, PumpState: pump, ClDoseSetpoint: 1.2},
    samples: [],
  }))};
}

test("expired polling preserves numbers only as old observations and disables both routes", async () => {
  let calls = 0;
  const view = page(async () => ++calls === 1 ?
    response(200, routes()) : response(0, null, "opaqueredirect"));
  vm.runInContext(liveSource, view.context);
  await drain();
  assert.equal(view.nodes.get("routea-level").textContent, "80.9 %");
  assert.equal(view.nodes.get("routea-reading-state").textContent, "LIVE OBSERVATION");
  view.timers.shift()();
  await drain();
  for (const route of ["routea", "routeb"]) {
    assert.match(view.nodes.get(`${route}-health`).textContent, /Sign-in expired.*last observed/);
    assert.equal(view.nodes.get(`${route}-reading-state`).textContent, "LAST OBSERVED / NOT LIVE");
    assert.equal(view.nodes.get(`${route}-pump`).textContent, "OFF (last observed)");
    assert.ok(view.actions[route].every(button => button.disabled));
  }
  assert.equal(view.nodes.get("session-expired").hidden, false);
  assert.equal(view.timers.length, 0);
});

test("stopped-worker observations remain stale even with a healthy console session", async () => {
  const view = page(async () => response(200, routes(true)));
  vm.runInContext(liveSource, view.context);
  await drain();
  for (const route of ["routea", "routeb"]) {
    assert.match(view.nodes.get(`${route}-health`).textContent, /^STALE \/ observed/);
    assert.equal(view.nodes.get(`${route}-reading-state`).textContent, "LAST OBSERVED / NOT LIVE");
    assert.ok(view.actions[route].every(button => button.disabled));
  }
  assert.equal(view.nodes.get("session-expired").hidden, true);
  assert.equal(view.timers.length, 1);
});

test("a transient network failure keeps read-only recovery polling enabled", async () => {
  let calls = 0;
  const view = page(async () => {
    if (++calls === 1) throw new TypeError("Failed to fetch");
    return response(200, routes());
  });
  vm.runInContext(liveSource, view.context);
  await drain();
  assert.equal(view.nodes.get("routea-reading-state").textContent, "NO LIVE OBSERVATION");
  assert.ok(view.actions.routea.every(button => button.disabled));
  view.timers.shift()();
  await drain();
  assert.equal(view.nodes.get("routea-reading-state").textContent, "LIVE OBSERVATION");
  assert.ok(view.actions.routea.every(button => !button.disabled));
});

test("motor and flow follow observed pump output and tank height follows measured level", async () => {
  let calls = 0;
  const view = page(async () => response(200, ++calls === 1 ?
    routes(false, 1, 61.4) : routes(false, 0, 61.5)));
  vm.runInContext(liveSource, view.context);
  await drain();
  for (const route of ["routea", "routeb"]) {
    assert.ok(view.nodes.get(`${route}-plant`).classList.contains("running"));
    assert.match(view.nodes.get(`${route}-machinery-state`).textContent, /PLC pump ON.*motor RUNNING/);
    assert.equal(Number(view.nodes.get(`${route}-water`).attributes.height), 1.38 * 61.4);
    assert.match(view.nodes.get(`${route}-plant`).attributes["aria-label"], /Observed PLC pump ON.*61.4 percent/);
    assert.ok(view.nodes.get(`${route}-plant`).classList.contains("observing"));
    assert.equal(view.nodes.get(`${route}-sensor-level`).textContent, "LEVEL 61.4 %");
    assert.equal(view.nodes.get(`${route}-sensor-chlorine`).textContent, "CL 1.08 mg/L");
  }
  view.timers.shift()();
  await drain();
  for (const route of ["routea", "routeb"]) {
    assert.ok(!view.nodes.get(`${route}-plant`).classList.contains("running"));
    assert.match(view.nodes.get(`${route}-machinery-state`).textContent, /pump OFF.*motor STOPPED/);
    assert.equal(Number(view.nodes.get(`${route}-water`).attributes.height), 1.38 * 61.5);
  }
  assert.equal(view.nodes.get("routeb-hmi-pump").textContent, "PUMP OFF");
});

for (const failure of ["stale", "network", "authentication", "missing"]) {
  test(`running machinery stops safely on ${failure} observations`, async () => {
    let calls = 0;
    const view = page(async () => {
      if (++calls === 1) return response(200, routes(false, 1));
      if (failure === "network") throw new TypeError("Failed to fetch");
      if (failure === "authentication") return response(0, null, "opaqueredirect");
      if (failure === "missing") return response(200, {routes:
        ["routea", "routeb"].map(route => ({route, ready: false, stale: true, error: "No live observation"}))});
      return response(200, routes(true, 1));
    });
    vm.runInContext(liveSource, view.context);
    await drain();
    assert.ok(view.nodes.get("routea-plant").classList.contains("running"));
    view.timers.shift()();
    await drain();
    for (const route of ["routea", "routeb"]) {
      assert.ok(!view.nodes.get(`${route}-plant`).classList.contains("running"));
      assert.ok(!view.nodes.get(`${route}-plant`).classList.contains("observing"));
      assert.match(view.nodes.get(`${route}-machinery-state`).textContent, /Animation paused.*last observed PLC pump ON/);
      assert.match(view.nodes.get(`${route}-plant`).attributes["aria-label"], /Last observed PLC pump ON.*Animation paused/);
      assert.ok(view.actions[route].every(button => button.disabled));
    }
    assert.equal(view.nodes.get("routeb-hmi-state").textContent, "NO LIVE DATA");
  });
}

for (const route of ["routea", "routeb"]) {
  for (const action of ["pump-on", "chlorine-up"]) {
    test(`${route} ${action} separates pending writes, PLC readback and verified restoration`, async () => {
      let submit;
      let pump = 0;
      let stage = "queued";
      const tag = action === "pump-on" ? "PumpOverride" : "ClDoseSetpoint";
      const value = action === "pump-on" ? 1 : 4;
      const baseline = action === "pump-on" ? 0 : 1.2;
      const view = page(async (url, options) => {
        if (url === "/api/live/routes") return response(200, routes(false, pump));
        if (options.method === "POST") return new Promise(resolve => { submit = resolve; });
        return response(200, {stage, action, durationSeconds: 20,
          baseline: {[tag]: baseline},
          observed: stage === "queued" || stage === "executing" ? undefined : {[tag]: value},
          restoredAt: stage === "completed-restored" ? new Date().toISOString() : undefined});
      });
      vm.runInContext(liveSource, view.context);
      await drain();
      const click = view.actions[route].find(button => button.dataset.action === action).listeners.click();
      assert.ok(view.nodes.get(`${route}-plant`).classList.contains("command-pending"));
      assert.ok(!view.nodes.get(`${route}-plant`).classList.contains("running"));
      assert.match(view.nodes.get(`${route}-plc-command`).textContent, new RegExp(`${tag} = ${value}`));
      assert.match(view.nodes.get(`${route}-plc-command`).textContent, /PLC readback: not confirmed/);
      submit(response(202, {runId: "test-run", stage: "queued"}));
      await click;
      for (stage of ["executing", "plc-observed", "completed-restored"]) {
        pump = stage === "executing" ? 0 : 1;
        view.timers.shift()();
        await drain();
        const kind = stage === "executing" ? "pending" : stage === "plc-observed" ? "applied" : "restored";
        assert.ok(view.nodes.get(`${route}-plant`).classList.contains(`command-${kind}`));
        assert.equal(view.nodes.get(`${route}-plant`).classList.contains("running"), pump === 1);
        if (route === "routeb") {
          assert.equal(view.nodes.get("routeb-hmi-state").textContent,
            stage === "executing" ? "EXECUTING" : stage === "plc-observed" ? "APPLIED" : "RESTORED");
        }
      }
      assert.match(view.nodes.get(`${route}-plc-command`).textContent, /PLC readback:/);
      assert.match(view.nodes.get(`${route}-plc-command`).textContent,
        new RegExp(`Verified restoration: ${tag} = ${baseline}`));
      assert.match(view.nodes.get(`${route}-plc-command`).textContent,
        route === "routea" ? /Direct Modbus write/ : /HMI request -> PLC write/);
      assert.ok(view.actions[route].every(button => !button.disabled));
    });
  }
}

test("cancelling confirmation never animates or submits a PLC command", async () => {
  let writes = 0;
  const view = page(async (url, options) => {
    if (options.method === "POST") writes++;
    return response(200, routes());
  });
  view.context.window.confirm = () => false;
  vm.runInContext(liveSource, view.context);
  await drain();
  await view.actions.routea[0].listeners.click();
  assert.equal(writes, 0);
  assert.ok(!view.nodes.get("routea-plant").classList.contains("command-pending"));
  assert.equal(view.nodes.get("routea-plc-command").textContent, "No PLC write requested.");
});

test("reloading during an active run recovers the actual PLC-confirmed command", async () => {
  const view = page(async url => {
    if (url === "/api/live/routes") {
      const data = routes(false, 1);
      data.routes[0].activeRunId = "existing-run";
      return response(200, data);
    }
    assert.match(url, /routea\/existing-run$/);
    return response(200, {stage: "plc-observed", action: "pump-on",
      baseline: {PumpOverride: 0}, observed: {PumpOverride: 1}});
  });
  vm.runInContext(liveSource, view.context);
  await drain();
  assert.ok(view.nodes.get("routea-plant").classList.contains("command-applied"));
  assert.ok(view.actions.routea.every(button => button.disabled));
  assert.match(view.nodes.get("routea-plc-command").textContent, /PLC readback: PumpOverride = 1/);
});

for (const stage of ["failed-restored", "interrupted-restored", "expired-unobserved"]) {
  test(`${stage} is never shown as a successful control action`, async () => {
    const view = page(async (url, options) => {
      if (url === "/api/live/routes") return response(200, routes());
      if (options.method === "POST") return response(202, {runId: "test-run", stage: "queued"});
      return response(200, {stage, action: "pump-on"});
    });
    vm.runInContext(liveSource, view.context);
    await drain();
    await view.actions.routeb[0].listeners.click();
    view.timers.shift()();
    await drain();
    assert.ok(view.nodes.get("routeb-plant").classList.contains("command-failed"));
    assert.ok(!view.nodes.get("routeb-plant").classList.contains("command-applied"));
    assert.ok(!view.nodes.get("routeb-plant").classList.contains("running"));
  });
}

test("a lost write response is unconfirmed, not successful, and is never replayed", async () => {
  let writes = 0;
  const view = page(async (url, options) => {
    if (options.method === "POST") {
      writes++;
      throw new TypeError("Failed to fetch");
    }
    return response(200, routes());
  });
  vm.runInContext(liveSource, view.context);
  await drain();
  await view.actions.routeb[0].listeners.click();
  assert.equal(writes, 1);
  assert.ok(view.nodes.get("routeb-plant").classList.contains("command-unknown"));
  assert.equal(view.nodes.get("routeb-hmi-state").textContent, "UNCONFIRMED");
  assert.ok(!view.nodes.get("routeb-plant").classList.contains("command-applied"));
});

test("a minimal recovery result preserves command details for the same run only", async () => {
  let stage = "plc-observed";
  const view = page(async (url, options) => {
    if (url === "/api/live/routes") return response(200, routes());
    if (options.method === "POST") return response(202, {runId: "test-run", stage: "queued"});
    if (stage === "plc-observed") return response(200, {runId: "test-run", stage, action: "pump-on",
      baseline: {PumpOverride: 0}, observed: {PumpOverride: 1}});
    return response(200, {runId: "test-run", stage, restoredAt: new Date().toISOString()});
  });
  vm.runInContext(liveSource, view.context);
  await drain();
  await view.actions.routea[0].listeners.click();
  view.timers.shift()();
  await drain();
  stage = "interrupted-restored";
  view.timers.shift()();
  await drain();
  assert.ok(view.nodes.get("routea-plant").classList.contains("command-failed"));
  assert.match(view.nodes.get("routea-plc-command").textContent, /Direct Modbus write: PumpOverride = 1/);
  assert.match(view.nodes.get("routea-plc-command").textContent, /Verified restoration: PumpOverride = 0/);
  await view.actions.routea[1].listeners.click();
  assert.match(view.nodes.get("routea-plc-command").textContent, /ClDoseSetpoint = 4/);
  assert.doesNotMatch(view.nodes.get("routea-plc-command").textContent, /PumpOverride|Verified restoration/);
});
