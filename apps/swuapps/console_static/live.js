"use strict";
(() => {
  const runs = new Map();
  const active = new Set();
  const observations = new Map();
  const commandStates = new Map();
  const routes = ["routea", "routeb"];
  const el = (route, name) => document.getElementById(`${route}-${name}`);
  const actions = (route) => document.querySelectorAll(`[data-route="${route}"]`);

  function command(route, result) {
    if (result) {
      const previous = commandStates.get(route);
      commandStates.set(route, result.runId && result.runId === previous?.runId ?
        {...previous, ...result} : result);
    }
    const state = commandStates.get(route);
    const stages = {
      idle: ["idle", "No control command selected.", "READY"],
      sending: ["pending", "Sending request / PLC change not confirmed.", "SENDING"],
      queued: ["pending", "Queued / PLC change not confirmed.", "QUEUED"],
      executing: ["pending", "Executing / waiting for PLC readback.", "EXECUTING"],
      "plc-observed": ["applied", "PLC readback confirmed / holding the temporary change.", "APPLIED"],
      "completed-restored": ["restored", "Completed / baseline restoration verified.", "RESTORED"],
      "failed-restored": ["failed", "Action failed / baseline restored. See execution details.", "FAILED"],
      "interrupted-restored": ["failed", "Action interrupted / baseline restored.", "INTERRUPTED"],
      "expired-unobserved": ["failed", "Command expired / no PLC change observed.", "EXPIRED"],
      unknown: ["unknown", "Request outcome unconfirmed / no automatic retry. See execution details.", "UNCONFIRMED"],
    };
    const stage = state?.stage || "idle";
    if (!stages[stage]) console.error("Unrecognized live command stage", stage);
    const [kind, label, screen] = stages[stage] || stages.unknown;
    const diagram = el(route, "plant");
    for (const name of ["pending", "applied", "restored", "failed", "unknown"]) {
      diagram.classList.toggle(`command-${name}`, name === kind);
    }
    const live = diagram.classList.contains("observing");
    const action = state?.action === "pump-on" ? "Pump override / " :
      state?.action === "chlorine-up" ? "Dose 4.0 / " : "";
    el(route, "command-state").textContent = `${live ? "" : "Live status unavailable / "}${action}${label}`;
    const write = state?.action === "pump-on" ? ["PumpOverride", 1] :
      state?.action === "chlorine-up" ? ["ClDoseSetpoint", 4.0] : null;
    if (write) {
      const [tag, value] = write;
      const path = route === "routea" ? "Direct Modbus write" : "Password-only HMI request -> PLC write";
      const baseline = state.baseline?.[tag];
      const observed = state.observed?.[tag];
      const restored = state.restoredAt && ["completed-restored", "failed-restored", "interrupted-restored"].includes(stage);
      el(route, "plc-command").textContent = `${path}: ${tag} = ${value}. ` +
        `Temporary hold: ${state.durationSeconds ?? 20} s. ` +
        (observed !== undefined ? `PLC readback: ${tag} = ${observed}. ` : "PLC readback: not confirmed. ") +
        (baseline !== undefined ? `${restored ? "Verified restoration" : "Restore target"}: ${tag} = ${baseline}.` :
          "Restore target: previous value, not yet reported.");
    } else {
      el(route, "plc-command").textContent = state ?
        "PLC command details unavailable for this run. See execution details." : "No PLC write requested.";
    }
    if (route === "routeb") el(route, "hmi-state").textContent = live ? screen : "NO LIVE DATA";
  }

  function machinery(route, state, stale) {
    const running = Boolean(state?.values.PumpState);
    const pump = running ? "ON" : "OFF";
    const diagram = el(route, "plant");
    diagram.classList.toggle("running", !stale && running);
    diagram.classList.toggle("observing", !stale);
    diagram.setAttribute("aria-label", state ?
      `${stale ? "Last observed" : "Observed"} PLC pump ${pump}. Tank level ${state.values.TankLevelPct.toFixed(1)} percent. ${stale ? "Animation paused." : "Motor and flow are simulated indications of the pump output."}` :
      "Simulated motor, pump and storage tank. Live observations unavailable; animation paused.");
    el(route, "machinery-state").textContent = stale ?
      `Animation paused / ${state ? `last observed PLC pump ${pump}` : "no live PLC observation"}` :
      `PLC pump ${pump} / simulated motor ${running ? "RUNNING" : "STOPPED"}`;
    el(route, "plc-state").textContent = stale ? "NO LIVE DATA" : "LIVE OUTPUT";
    el(route, "sensor-level").textContent = state ? `LEVEL ${state.values.TankLevelPct.toFixed(1)} %` : "LEVEL -- %";
    el(route, "sensor-chlorine").textContent = state ? `CL ${state.values.ClResidual.toFixed(2)} mg/L` : "CL -- mg/L";
    if (route === "routeb") el(route, "hmi-pump").textContent = state ? `PUMP ${pump}` : "PUMP --";
    command(route);
  }

  function trend(samples, tag, maximum) {
    if (samples.length < 2) return "";
    const first = Date.parse(samples[0].at);
    const span = Math.max(1, Date.parse(samples[samples.length - 1].at) - first);
    return samples.map((sample, index) => {
      const x = 320 * (Date.parse(sample.at) - first) / span;
      const y = 98 - 96 * Math.min(maximum, Math.max(0, sample[tag])) / maximum;
      return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
  }

  async function job(route) {
    const id = runs.get(route);
    if (!id) return;
    const result = await api(`/api/live/commands/${route}/${id}`);
    if (runs.get(route) !== id) return;
    el(route, "job").textContent = JSON.stringify(result, null, 2);
    command(route, result);
    if (["completed-restored", "failed-restored", "interrupted-restored", "expired-unobserved"].includes(result.stage)) {
      active.delete(route);
    }
    document.querySelector(`[data-evidence="${route}"]`).disabled = false;
  }

  async function refresh() {
    let retry = true;
    try {
      const data = await api("/api/live/routes", {signal: AbortSignal.timeout(15000)});
      for (const state of data.routes) {
        const route = state.route;
        const stale = state.stale || !state.ready;
        el(route, "card").classList.toggle("stale", stale);
        el(route, "health").textContent = state.ready ?
          `${stale ? "STALE" : "LIVE"} / observed ${new Date(state.observedAt).toLocaleString()} / ${state.path}` : state.error;
        el(route, "reading-state").textContent = !stale ? "LIVE OBSERVATION" :
          (state.ready || observations.has(route) ? "LAST OBSERVED / NOT LIVE" : "NO LIVE OBSERVATION");
        if (state.ready) {
          observations.set(route, state);
          const value = state.values;
          const height = 1.38 * Math.min(100, Math.max(0, value.TankLevelPct));
          el(route, "water").setAttribute("height", height);
          el(route, "water").setAttribute("y", 149 - height);
          el(route, "level").textContent = `${value.TankLevelPct.toFixed(1)} %`;
          el(route, "chlorine").textContent = `${value.ClResidual.toFixed(2)} mg/L`;
          el(route, "pump").textContent = `${value.PumpState ? "ON" : "OFF"}${stale ? " (last observed)" : ""}`;
          el(route, "dose").textContent = value.ClDoseSetpoint.toFixed(2);
          el(route, "tank-trend").setAttribute("d", trend(state.samples, "TankLevelPct", 100));
          el(route, "chlorine-trend").setAttribute("d", trend(state.samples, "ClResidual", 5));
          if (state.activeRunId && commandStates.get(route)?.stage !== "sending") {
            runs.set(route, state.activeRunId);
            active.add(route);
          }
        }
        machinery(route, state.ready ? state : observations.get(route), stale);
        await job(route);
        actions(route).forEach((button) => { button.disabled = stale || active.has(route); });
      }
    } catch (error) {
      retry = !error.authenticationRequired;
      for (const route of routes) {
        const last = observations.get(route);
        machinery(route, last, true);
        el(route, "card").classList.add("stale");
        el(route, "health").textContent = `UNAVAILABLE / ${error.message}${last ?
          ` / last observed ${new Date(last.observedAt).toLocaleString()}` : ""}`;
        el(route, "reading-state").textContent = last ? "LAST OBSERVED / NOT LIVE" : "NO LIVE OBSERVATION";
        if (last) el(route, "pump").textContent = `${last.values.PumpState ? "ON" : "OFF"} (last observed)`;
        actions(route).forEach((button) => { button.disabled = true; });
      }
    } finally {
      if (retry) window.setTimeout(refresh, 5000);
    }
  }

  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const route = button.dataset.route;
      if (!window.confirm(`${route === "routea" ? "Route A" : "Route B"}: change an actual lab PLC setpoint for 20 seconds, then restore it?`)) return;
      active.add(route);
      runs.delete(route);
      command(route, {stage: "sending", action: button.dataset.action});
      actions(route).forEach((action) => { action.disabled = true; });
      try {
        const result = await api("/api/live/commands", {method: "POST",
          body: JSON.stringify({route, action: button.dataset.action, confirmed: true})});
        runs.set(route, result.runId);
        command(route, {...result, stage: result.stage || "queued", action: button.dataset.action});
        el(route, "job").textContent = JSON.stringify(result, null, 2);
        el(route, "evidence").textContent = "Queued only. Execution, PLC readback, ingestion and alert evidence are separate.";
      } catch (error) {
        command(route, {stage: "unknown", action: button.dataset.action, runId: error.details?.runId});
        if (error.details?.runId) {
          runs.set(route, error.details.runId);
          el(route, "job").textContent = JSON.stringify(error.details, null, 2);
        } else {
          active.delete(route);
        }
        message(error.message, true);
      }
    });
  });
  document.querySelectorAll("[data-evidence]").forEach((button) => {
    button.addEventListener("click", async () => {
      const route = button.dataset.evidence;
      button.disabled = true;
      try {
        const result = await api(`/api/live/commands/${route}/${runs.get(route)}/evidence`);
        el(route, "evidence").textContent = `Sentinel: ${result.sentinel}. Alert: ${result.alert}. Queried ${new Date(result.queriedAt).toLocaleTimeString()}.`;
      } catch (error) {
        el(route, "evidence").textContent = `Evidence query failed: ${error.message}`;
      } finally {
        button.disabled = false;
      }
    });
  });
  refresh();
})();
