/* Recertia console — Pilot Compose / Run + Tower / Ops */

const $ = (sel) => document.querySelector(sel);
const state = {
  draft: null,
  formSource: "manual",
};

function escapeHtml(value) {
  const table = {
    "&": "\u0026amp;",
    "<": "\u0026lt;",
    ">": "\u0026gt;",
    '"': "\u0026quot;",
    "'": "\u0026#39;",
  };
  return String(value ?? "").replace(/[&<>"']/g, (ch) => table[ch]);
}

function apiKey() { return $("#apiKey").value.trim(); }
function tenantHeader() { return $("#tenantHeader").value.trim(); }

function looksAbsolutePath(s) {
  const v = (s || "").trim();
  if (!v) return false;
  if (v.startsWith("/") || v.startsWith("\\")) return true;
  if (/^[A-Za-z]:[\\/]/.test(v)) return true;
  if (v.startsWith("//") || v.startsWith("\\\\")) return true;
  return false;
}

function selectedWorkspaceId() {
  return ($("#workspaceSelect") && $("#workspaceSelect").value.trim()) || "";
}

function workspaceSubpath() {
  return ($("#workspaceSubpath") && $("#workspaceSubpath").value.trim()) || "";
}

function buildRunSubmitBody(goal, mode) {
  const body = {
    goal,
    task_class: goal.task_class || "repo-chore",
    mode,
    budget: { max_attempts: 2 },
  };
  const ws = selectedWorkspaceId();
  if (ws) {
    body.workspace_id = ws;
    body.workdir = workspaceSubpath();
  }
  return body;
}

async function loadWorkspaces() {
  const sel = $("#workspaceSelect");
  if (!sel) return;
  const current = sel.value;
  try {
    const data = await api("/v1/workspaces");
    const enabled = (data.workspaces || []).filter((w) => w.enabled !== false);
    sel.innerHTML = `<option value="">sandbox — new empty workdir</option>`;
    for (const w of enabled) {
      const opt = document.createElement("option");
      opt.value = w.workspace_id;
      opt.textContent = `${w.display_name} (${w.workspace_id}) — ${w.host_root}`;
      sel.appendChild(opt);
    }
    if (current && [...sel.options].some((o) => o.value === current)) sel.value = current;
    if ($("#workspacesOut")) {
      $("#workspacesOut").textContent = JSON.stringify(data.workspaces || [], null, 2);
    }
  } catch (e) {
    if ($("#workspacesOut")) $("#workspacesOut").textContent = String(e);
  }
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "content-type": "application/json" }, opts.headers || {});
  if (apiKey()) headers["X-API-Key"] = apiKey();
  if (tenantHeader()) headers["X-Recertia-Tenant"] = tenantHeader();
  const res = await fetch(path, { ...opts, headers, credentials: "same-origin" });
  const text = await res.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = { raw: text }; }
  if (!res.ok) {
    const detail = body?.detail;
    const msg = typeof detail === "string" ? detail : (detail && JSON.stringify(detail)) || body?.error?.message || res.statusText;
    throw new Error(msg);
  }
  return body;
}

function showView(name) {
  document.querySelectorAll(".view").forEach((el) => el.classList.remove("active"));
  document.querySelectorAll(".nav button[data-view]").forEach((el) => el.classList.remove("active"));
  $(`#view-${name}`).classList.add("active");
  document.querySelector(`.nav button[data-view="${name}"]`).classList.add("active");
  if (name === "pilot" || name === "auth" || name === "programs") {
    loadWorkspaces().catch(() => {});
  }
}

document.querySelectorAll(".nav button[data-view]").forEach((btn) => {
  btn.addEventListener("click", () => showView(btn.dataset.view));
});

function setPilotMode(mode) {
  document.querySelectorAll("[data-pilot-mode]").forEach((b) => {
    b.classList.toggle("active", b.dataset.pilotMode === mode);
  });
  $("#pilot-compose").classList.toggle("hidden", mode !== "compose");
  $("#pilot-run").classList.toggle("hidden", mode !== "run");
}

document.querySelectorAll("[data-pilot-mode]").forEach((btn) => {
  btn.addEventListener("click", () => {
    setPilotMode(btn.dataset.pilotMode);
    if (btn.dataset.pilotMode === "run") loadWorkspaces().catch(() => {});
  });
});

function desiredValue(prefill) {
  if (prefill.kind === "file_contains") {
    return `${prefill.path || ""}${prefill.pattern ? "|" + prefill.pattern : ""}`;
  }
  if (prefill.kind === "command") return prefill.run || "";
  return prefill.path || prefill.value || "";
}

function addDesiredRow(prefill = {}) {
  const row = document.createElement("div");
  row.className = "desired-row";
  row.innerHTML = `
    <input placeholder="id" value="${escapeHtml(prefill.id || "")}" data-f="id" />
    <select data-f="kind">
      <option value="file_exists">file_exists</option>
      <option value="file_contains">file_contains</option>
      <option value="command">command</option>
    </select>
    <input placeholder="path / path|pattern / command" value="${escapeHtml(desiredValue(prefill))}" data-f="value" />
    <button type="button" class="danger">×</button>`;
  if (prefill.kind) row.querySelector('[data-f="kind"]').value = prefill.kind;
  row.querySelector("button").onclick = () => row.remove();
  $("#desiredList").appendChild(row);
}

function addConstraintRow(prefill = {}) {
  const row = document.createElement("div");
  row.className = "desired-row";
  const val = Array.isArray(prefill.value) ? prefill.value.join(",") : (prefill.value ?? "");
  row.innerHTML = `
    <input placeholder="id" value="${escapeHtml(prefill.id || "")}" data-f="id" />
    <select data-f="kind">
      <option value="must_not_modify">must_not_modify</option>
      <option value="must_pass_command">must_pass_command</option>
      <option value="no_external_effects">no_external_effects</option>
    </select>
    <input placeholder="paths (comma) / command / true" value="${escapeHtml(val)}" data-f="value" />
    <button type="button" class="danger">×</button>`;
  if (prefill.kind) row.querySelector('[data-f="kind"]').value = prefill.kind;
  row.querySelector("button").onclick = () => row.remove();
  $("#constraintList").appendChild(row);
}

$("#addDesired").onclick = () => addDesiredRow({ id: `d${$("#desiredList").children.length + 1}`, kind: "file_exists" });
$("#addConstraint").onclick = () => addConstraintRow({ id: `c${$("#constraintList").children.length + 1}`, kind: "must_not_modify" });
addDesiredRow({ id: "d1", kind: "file_exists", path: ".gitignore" });

function buildConstraints() {
  return [...$("#constraintList").children].map((row, i) => {
    const id = row.querySelector('[data-f="id"]').value || `c${i + 1}`;
    const kind = row.querySelector('[data-f="kind"]').value;
    const raw = row.querySelector('[data-f="value"]').value.trim();
    if (kind === "must_not_modify") {
      return { id, kind, value: raw.split(",").map((s) => s.trim()).filter(Boolean), weight: 1.0 };
    }
    if (kind === "no_external_effects") {
      return { id, kind, value: raw || "true", weight: 1.0 };
    }
    return { id, kind, value: raw, weight: 1.0 };
  }).filter((c) => {
    if (c.kind === "must_not_modify") return Array.isArray(c.value) && c.value.length;
    return !!c.value;
  });
}

function buildGoal() {
  const desired = [...$("#desiredList").children].map((row, i) => {
    const id = row.querySelector('[data-f="id"]').value || `d${i + 1}`;
    const kind = row.querySelector('[data-f="kind"]').value;
    const value = row.querySelector('[data-f="value"]').value;
    const base = { id, kind, weight: 1.0 };
    if (kind === "file_exists") return { ...base, path: value };
    if (kind === "file_contains") {
      return { ...base, path: value.split("|")[0], pattern: value.split("|")[1] || value };
    }
    return { ...base, run: value };
  });
  return {
    goal_id: `console-${Date.now()}`,
    desired,
    constraints: buildConstraints(),
    context: $("#goalContext").value || null,
    task_class: $("#taskClass").value || "repo-chore",
  };
}

function renderDraft(draft) {
  state.draft = draft;
  $("#draftPanel").classList.remove("hidden");
  $("#draftSource").textContent = draft.source || "draft";
  $("#draftDisclaimer").textContent = draft.disclaimer || "";
  const w = $("#draftWarnings");
  w.innerHTML = "";
  for (const warn of draft.warnings || []) {
    const el = document.createElement("div");
    el.className = `warn ${warn.severity || "warn"}`;
    el.textContent = `[${warn.severity}] ${warn.message}`;
    w.appendChild(el);
  }
  const box = $("#draftDesired");
  box.innerHTML = "";
  (draft.desired || []).forEach((d, i) => {
    const el = document.createElement("label");
    el.className = "draft-item";
    const summary = d.kind === "file_contains"
      ? `${d.path}|${d.pattern}`
      : (d.path || d.run || "");
    el.innerHTML = `
      <input type="checkbox" data-draft-d="${i}" ${d.selected !== false ? "checked" : ""} />
      <div>
        <strong>${escapeHtml(d.id)}</strong> · ${escapeHtml(d.kind)}
        <div class="meta">${escapeHtml(summary)}</div>
        <div class="muted">${escapeHtml(d.why || "")}${d.risk ? " — risk: " + escapeHtml(d.risk) : ""}</div>
      </div>`;
    box.appendChild(el);
  });
  const cbox = $("#draftConstraints");
  cbox.innerHTML = "";
  if (!(draft.constraints || []).length) {
    cbox.innerHTML = `<p class="muted">None</p>`;
  }
  (draft.constraints || []).forEach((c, i) => {
    const el = document.createElement("label");
    el.className = "draft-item";
    el.innerHTML = `
      <input type="checkbox" data-draft-c="${i}" ${c.selected !== false ? "checked" : ""} />
      <div>
        <strong>${escapeHtml(c.id)}</strong> · ${escapeHtml(c.kind)}
        <div class="meta">${escapeHtml(Array.isArray(c.value) ? c.value.join(", ") : c.value)}</div>
        <div class="muted">${escapeHtml(c.why || "")}</div>
      </div>`;
    cbox.appendChild(el);
  });
  const pack = draft.pack || [];
  $("#draftPackWrap").classList.toggle("hidden", !pack.length);
  $("#applyPack0").classList.toggle("hidden", !pack.length);
  $("#savePackAsProgram").classList.toggle("hidden", !(pack.length || (draft.decompositions || []).length));
  const pbox = $("#draftPack");
  pbox.innerHTML = "";
  pack.forEach((p, i) => {
    const card = document.createElement("div");
    card.className = "pack-card";
    card.innerHTML = `<strong>${i + 1}. ${escapeHtml(p.title)}</strong>
      <div class="muted">${escapeHtml(p.context || "")}</div>
      <div class="meta">${escapeHtml((p.desired || []).map((d) => d.id).join(", ") || "(no desired)")}</div>
      <button type="button" class="secondary" data-pack-apply="${i}">Apply this pack goal</button>`;
    pbox.appendChild(card);
  });
  pbox.querySelectorAll("[data-pack-apply]").forEach((btn) => {
    btn.onclick = () => applyPackItem(Number(btn.dataset.packApply));
  });
}

function applyDesiredList(desired) {
  $("#desiredList").innerHTML = "";
  for (const d of desired) addDesiredRow(d);
  if (!desired.length) addDesiredRow({ id: "d1", kind: "file_exists", path: ".gitignore" });
}

function applyConstraintList(constraints) {
  $("#constraintList").innerHTML = "";
  for (const c of constraints || []) addConstraintRow(c);
}

function applyPackItem(index) {
  const pack = state.draft?.pack?.[index];
  if (!pack) return;
  if (pack.context) $("#goalContext").value = pack.context;
  applyDesiredList(pack.desired || []);
  applyConstraintList(pack.constraints || []);
  state.formSource = `pack:${pack.title}`;
  $("#formSourceHint").textContent = `(from pack: ${pack.title})`;
  setPilotMode("run");
}

$("#suggestCriteria").onclick = async () => {
  try {
    const body = {
      context: $("#goalContext").value.trim(),
      task_class: $("#taskClass").value || "repo-chore",
      use_model: $("#useModel").checked,
    };
    if (!body.context) {
      alert("Enter context first");
      return;
    }
    $("#draftDisclaimer").textContent = "Suggesting…";
    $("#draftPanel").classList.remove("hidden");
    const draft = await api("/v1/goals/suggest", { method: "POST", body: JSON.stringify(body) });
    renderDraft(draft);
  } catch (e) {
    alert(e);
  }
};

$("#dismissDraft").onclick = () => {
  state.draft = null;
  $("#draftPanel").classList.add("hidden");
};

$("#applyDraft").onclick = () => {
  if (!state.draft) return;
  const desired = [];
  document.querySelectorAll("[data-draft-d]").forEach((cb) => {
    if (!cb.checked) return;
    desired.push(state.draft.desired[Number(cb.dataset.draftD)]);
  });
  const constraints = [];
  document.querySelectorAll("[data-draft-c]").forEach((cb) => {
    if (!cb.checked) return;
    constraints.push(state.draft.constraints[Number(cb.dataset.draftC)]);
  });
  if (!desired.length) {
    alert("Select at least one desired state (or apply a pack goal)");
    return;
  }
  const blocked = (state.draft.warnings || []).some((w) => w.severity === "block");
  if (blocked && !confirm("Draft has blocking warnings. Apply anyway?")) return;
  applyDesiredList(desired);
  applyConstraintList(constraints);
  state.formSource = `draft:${state.draft.source}`;
  $("#formSourceHint").textContent = `(accepted ${state.draft.source} draft)`;
  setPilotMode("run");
};

$("#applyPack0").onclick = () => applyPackItem(0);

$("#savePackAsProgram").onclick = async () => {
  try {
    const draft = state.draft;
    if (!draft) return;
    const decomp = (draft.decompositions && draft.decompositions[0]) || null;
    const steps = decomp
      ? decomp.steps
      : (draft.pack || []).map((p, i) => ({
          title: p.title,
          context: p.context,
          desired: p.desired,
          constraints: p.constraints,
          role: i === 0 ? "characterization" : i === (draft.pack.length - 1) ? "behaviour_lock" : "structural",
        }));
    if (!steps.length) {
      alert("No pack/decomposition to save");
      return;
    }
    const created = await api("/v1/programs/from-pack", {
      method: "POST",
      body: JSON.stringify({
        title: `From Compose: ${(draft.context || "").slice(0, 48)}`,
        intent: draft.context || "",
        task_class: draft.task_class || "repo-chore",
        decomposition: (decomp && decomp.decomposition) || "by_risk",
        steps,
      }),
    });
    alert(`Saved program ${created.program.program_id} (draft). Open Programs to accept.`);
    showView("programs");
    await openProgram(created.program.program_id);
  } catch (e) {
    alert(e);
  }
};

$("#previewGoal").onclick = async () => {
  try {
    const out = await api("/v1/goals/preview", { method: "POST", body: JSON.stringify({ goal: buildGoal() }) });
    $("#previewOut").textContent = JSON.stringify({ form_source: state.formSource, ...out }, null, 2);
  } catch (e) { $("#previewOut").textContent = String(e); }
};

$("#submitRun").onclick = async () => {
  try {
    const sub = workspaceSubpath();
    if (looksAbsolutePath(sub)) {
      throw new Error("Subpath must be relative to the registered workspace (absolute paths rejected)");
    }
    if (!selectedWorkspaceId() && looksAbsolutePath(sub)) {
      throw new Error("Sandbox workdir must be relative");
    }
    const goal = buildGoal();
    const mode = $("#runMode").value;
    const body = buildRunSubmitBody(goal, mode);
    const res = await fetch("/v1/runs", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(apiKey() ? { "X-API-Key": apiKey() } : {}),
        ...(state.session ? { "X-Recertia-Session": state.session } : {}),
        ...(tenantHeader() ? { "X-Recertia-Tenant": tenantHeader() } : {}),
      },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    $("#previewOut").textContent = JSON.stringify({ status: res.status, form_source: state.formSource, ...data }, null, 2);
    if (mode === "async" && data.run_id) streamEvents(data.run_id);
  } catch (e) { $("#previewOut").textContent = String(e); }
};

async function loadTemplates() {
  try {
    const data = await api("/v1/templates");
    const sel = $("#templateSelect");
    for (const t of data.templates || []) {
      const opt = document.createElement("option");
      opt.value = t.id; opt.textContent = t.title;
      sel.appendChild(opt);
    }
  } catch { /* key may be empty on first paint */ }
}

$("#templateSelect").onchange = async () => {
  const id = $("#templateSelect").value;
  if (!id) return;
  const data = await api(`/v1/templates/${id}`);
  const g = data.goal;
  $("#goalContext").value = g.context || "";
  $("#taskClass").value = g.task_class || "repo-chore";
  applyDesiredList(g.desired || []);
  applyConstraintList(g.constraints || []);
  state.formSource = `template:${id}`;
  $("#formSourceHint").textContent = `(template ${id})`;
};

async function refreshRuns() {
  const data = await api("/v1/runs");
  const tb = $("#runsTable tbody");
  tb.innerHTML = "";
  for (const r of data.items || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(r.run_id)}</td><td>${escapeHtml(r.task_class)}</td><td>${escapeHtml(r.terminal || r.status)}</td><td>${escapeHtml(r.cost_usd ?? "—")}</td><td><button class="secondary">Open</button></td>`;
    tr.querySelector("button").onclick = () => openRun(r.run_id);
    tb.appendChild(tr);
  }
}

async function openRun(runId) {
  const rec = await api(`/v1/runs/${runId}`);
  $("#runDetail").classList.remove("hidden");
  $("#runDetail").textContent = JSON.stringify(rec, null, 2);
  try {
    const tr = await api(`/v1/runs/${runId}/transcript`);
    $("#runDetail").textContent += "\n\n--- transcript ---\n" + JSON.stringify(tr, null, 2);
  } catch { /* optional */ }
  streamEvents(runId);
}

function streamEvents(runId) {
  const el = $("#eventStream");
  el.classList.remove("hidden");
  el.textContent = `SSE ${runId}…\n`;
  const headers = {};
  if (apiKey()) headers["X-API-Key"] = apiKey();
  fetch(`/v1/runs/${runId}/events`, { headers, credentials: "same-origin" }).then(async (res) => {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      el.textContent += dec.decode(value);
      el.scrollTop = el.scrollHeight;
    }
  }).catch((e) => { el.textContent += String(e); });
}

$("#refreshRuns").onclick = () => refreshRuns().catch((e) => alert(e));

async function refreshSkills() {
  const data = await api("/v1/skills");
  const tb = $("#skillsTable tbody");
  tb.innerHTML = "";
  for (const s of data.items || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(s.skill_id)}</td><td>${escapeHtml(s.version)}</td><td>${escapeHtml(s.lifecycle)}</td><td>${escapeHtml(s.active)}</td><td>${escapeHtml((s.live_mix && s.live_mix.reason) || "—")}</td><td><button class="secondary">View</button> <button class="primary">Promote</button></td>`;
    tr.querySelectorAll("button")[0].onclick = async () => {
      const d = await api(`/v1/skills/${s.skill_id}/versions/${s.version}`);
      $("#skillDetail").classList.remove("hidden");
      $("#skillDetail").textContent = formatSkillDetail(d);
    };
    tr.querySelectorAll("button")[1].onclick = async () => {
      if (!confirm(`Promote ${s.skill_id}@v${s.version}? Golden gate required.`)) return;
      const d = await api(`/v1/skills/${s.skill_id}/versions/${s.version}/promote`, { method: "POST", body: "{}" });
      $("#skillDetail").classList.remove("hidden");
      $("#skillDetail").textContent = JSON.stringify(d, null, 2);
    };
    tb.appendChild(tr);
  }
}
$("#refreshSkills").onclick = () => refreshSkills().catch((e) => alert(e));

function formatSkillDetail(d) {
  const identity = d.identity || {};
  const live = d.live_mix || {};
  const banner = live.reason
    ? `Live mix: ${live.reason}` +
      (live.consecutive_field_failures
        ? ` · ${live.consecutive_field_failures} consecutive field failure(s)`
        : "") +
      (live.reason === "shadow_trial"
        ? " — certified, not steering live traffic until contribution is non-negative."
        : "") +
      (live.reason === "quarantined"
        ? " — pulled off the live mix after consecutive field failures."
        : "")
    : "";
  const blocks = [
    banner,
    banner ? "" : "",
    "Authoring (Provenance — frozen)",
    JSON.stringify(identity.authoring || {}, null, 2),
    "",
    "Applications (SkillStats.apply_diversity — rebuildable)",
    JSON.stringify(identity.applications || {}, null, 2),
    "",
    "Live mix",
    JSON.stringify(live, null, 2),
    "",
    "--- full document ---",
    JSON.stringify({ version: d.version, status: d.status, stats: d.stats }, null, 2),
  ].filter((line, i, arr) => !(line === "" && arr[i - 1] === ""));
  return blocks.join("\n");
}

async function refreshTower() {
  const summary = await api("/v1/console/tower-summary");
  $("#towerSummary").innerHTML = `
    <div class="card"><div class="k">active_cap_pressure</div><div class="v">${escapeHtml(summary.active_cap_pressure ?? "—")}</div></div>
    <div class="card"><div class="k">composition depth</div><div class="v">${escapeHtml(summary.mean_composition_depth ?? "—")}</div></div>
    <div class="card"><div class="k">practice_conversion</div><div class="v">${escapeHtml(summary.practice_conversion ?? summary.practice_conversion_unavailable ?? "—")}</div></div>
    <div class="card"><div class="k">pending proposals</div><div class="v">${escapeHtml(summary.pending_proposals)}</div></div>`;
  const props = await api("/v1/proposals");
  const tb = $("#proposalsTable tbody");
  tb.innerHTML = "";
  for (const p of props.items || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(p.proposal_id)}</td><td>${escapeHtml(p.kind)}</td><td>${escapeHtml(p.skill_id)}</td><td>${escapeHtml(p.status)}</td>
      <td><button class="secondary">Open</button>
      <button class="primary" ${p.status !== "pending" ? "disabled" : ""}>Approve</button>
      <button class="danger" ${p.status !== "pending" ? "disabled" : ""}>Reject</button></td>`;
    const [openBtn, okBtn, noBtn] = tr.querySelectorAll("button");
    openBtn.onclick = () => {
      $("#proposalDetail").classList.remove("hidden");
      $("#proposalDetail").textContent = JSON.stringify(p, null, 2);
      if (p.payload?.replay_pack) {
        $("#proposalDetail").textContent += "\n\n--- replay_pack ---\n" + JSON.stringify(p.payload.replay_pack, null, 2);
      }
    };
    okBtn.onclick = async () => {
      const d = await api(`/v1/proposals/${p.proposal_id}/decision`, {
        method: "POST", body: JSON.stringify({ decision: "approve", note: "console" }),
      });
      $("#proposalDetail").classList.remove("hidden");
      $("#proposalDetail").textContent = JSON.stringify(d, null, 2);
      refreshTower();
    };
    noBtn.onclick = async () => {
      await api(`/v1/proposals/${p.proposal_id}/decision`, {
        method: "POST", body: JSON.stringify({ decision: "reject", note: "console" }),
      });
      refreshTower();
    };
    tb.appendChild(tr);
  }
}
$("#refreshProposals").onclick = () => refreshTower().catch((e) => alert(e));
$("#runCurator").onclick = async () => {
  const d = await api("/v1/jobs/curator/run", { method: "POST", body: JSON.stringify({ dry_run: true }) });
  $("#proposalDetail").classList.remove("hidden");
  $("#proposalDetail").textContent = JSON.stringify(d, null, 2);
  refreshTower();
};
$("#runPractice").onclick = async () => {
  const d = await api("/v1/jobs/practice/run", { method: "POST", body: JSON.stringify({ dry_run: true }) });
  $("#proposalDetail").classList.remove("hidden");
  $("#proposalDetail").textContent = JSON.stringify(d, null, 2);
};

$("#refreshMetrics").onclick = async () => {
  try {
    const report = await api("/v1/metrics/report");
    const canary = await api("/v1/metrics/canary");
    const dash = await api("/v1/metrics/dashboard");
    $("#metricsOut").textContent = JSON.stringify({ report, canary, dashboard: dash }, null, 2);
  } catch (e) { $("#metricsOut").textContent = String(e); }
};

$("#devLogin").onclick = async () => {
  const d = await api("/v1/auth/dev-login", {
    method: "POST",
    body: JSON.stringify({
      user_id: "dev-operator",
      roles: ["operator"],
      tenants: ["default", "tenant-b"],
    }),
  });
  $("#authOut").textContent = JSON.stringify(d, null, 2);
};
$("#loadMe").onclick = async () => {
  try { $("#authOut").textContent = JSON.stringify(await api("/v1/me"), null, 2); }
  catch (e) { $("#authOut").textContent = String(e); }
};
$("#logout").onclick = async () => {
  await api("/v1/auth/logout", { method: "POST", body: "{}" });
  $("#authOut").textContent = "logged out";
};
$("#doSwitch").onclick = async () => {
  const d = await api("/v1/auth/switch-tenant", {
    method: "POST",
    body: JSON.stringify({ tenant_id: $("#switchTenant").value }),
  });
  $("#tenantHeader").value = d.active_tenant;
  $("#authOut").textContent = JSON.stringify(d, null, 2);
};

$("#refreshWorkspaces").onclick = () => loadWorkspaces().catch((e) => {
  $("#workspacesOut").textContent = String(e);
});
$("#registerWorkspace").onclick = async () => {
  try {
    const host = $("#wsHostRoot").value.trim();
    if (!host) throw new Error("host_root required");
    const d = await api("/v1/workspaces", {
      method: "POST",
      body: JSON.stringify({
        workspace_id: $("#wsId").value.trim(),
        display_name: $("#wsName").value.trim() || $("#wsId").value.trim(),
        host_root: host,
        notes: $("#wsNotes").value.trim() || null,
      }),
    });
    $("#workspacesOut").textContent = JSON.stringify(d, null, 2);
    await loadWorkspaces();
  } catch (e) {
    $("#workspacesOut").textContent = String(e);
  }
};

setPilotMode("compose");
loadTemplates();
loadWorkspaces().catch(() => {});

/* ----- Migration programs (GP0 board) ----- */
state.programId = null;
state.program = null;

async function refreshPrograms() {
  const data = await api("/v1/programs");
  const tb = $("#programsTable tbody");
  tb.innerHTML = "";
  for (const p of data.programs || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(p.program_id)}</td><td>${escapeHtml(p.title)}</td><td>${escapeHtml(p.status)}</td><td><button class="secondary">Open</button></td>`;
    tr.querySelector("button").onclick = () => openProgram(p.program_id);
    tb.appendChild(tr);
  }
}

async function openProgram(id) {
  const data = await api(`/v1/programs/${id}`);
  state.programId = id;
  state.program = data.program;
  $("#programBoard").classList.remove("hidden");
  $("#programTitle").textContent = data.program.title;
  $("#programMeta").textContent = `${data.program.program_id} · ${data.program.status} · freeze=${data.program.freeze_enforcement} · handoff=${data.program.handoff}`;
  renderProgramSteps(data.program, data.warnings || []);
  $("#programOut").textContent = JSON.stringify(data.warnings || [], null, 2);
}

function renderProgramSteps(prog, warnings) {
  const box = $("#programSteps");
  box.innerHTML = "";
  (prog.steps || []).forEach((step) => {
    const el = document.createElement("div");
    el.className = "program-step";
    el.innerHTML = `
      <div class="program-step-head">
        <strong>${escapeHtml(step.ordinal)}. ${escapeHtml(step.title)}</strong>
        <span class="muted">${escapeHtml(step.role)} · ${escapeHtml(step.status)}</span>
      </div>
      <div class="muted">freeze: ${escapeHtml((step.freeze_paths || []).join(", ") || "—")} · mutate: ${escapeHtml((step.mutate_paths || []).join(", ") || "—")}</div>
      <div class="actions">
        <button type="button" class="secondary" data-act="preview">Preview</button>
        <button type="button" class="secondary" data-act="envelope">Run envelope</button>
        <button type="button" class="primary" data-act="submit-bind">Submit + bind</button>
        <select data-workspace><option value="">sandbox / relative</option></select>
        <input data-workdir placeholder="subpath or relative workdir" value="" />
      </div>`;
    const wsSel = el.querySelector("[data-workspace]");
    const pilotSel = $("#workspaceSelect");
    if (pilotSel) {
      for (const opt of [...pilotSel.options]) {
        if (!opt.value) continue;
        const clone = opt.cloneNode(true);
        wsSel.appendChild(clone);
      }
    }
    const readBind = () => ({
      workdir: el.querySelector("[data-workdir]").value,
      workspace_id: wsSel.value || null,
    });
    el.querySelector('[data-act="preview"]').onclick = () => previewProgramStep(step.step_id);
    el.querySelector('[data-act="envelope"]').onclick = () => {
      const b = readBind();
      envelopeProgramStep(step.step_id, b.workdir, b.workspace_id);
    };
    el.querySelector('[data-act="submit-bind"]').onclick = () => {
      const b = readBind();
      submitBindProgramStep(step.step_id, b.workdir, b.workspace_id);
    };
    box.appendChild(el);
  });
  if (warnings && warnings.length) {
    const w = document.createElement("p");
    w.className = "muted";
    w.textContent = warnings.map((x) => x.code).join(", ");
    box.appendChild(w);
  }
}

async function previewProgramStep(stepId) {
  try {
    const out = await api(`/v1/programs/${state.programId}/steps/${stepId}/preview`, { method: "POST", body: "{}" });
    $("#programOut").textContent = JSON.stringify(out, null, 2);
    await openProgram(state.programId);
  } catch (e) {
    $("#programOut").textContent = String(e);
  }
}

async function envelopeProgramStep(stepId, workdir, workspaceId) {
  try {
    const out = await api(`/v1/programs/${state.programId}/steps/${stepId}/run`, {
      method: "POST",
      body: JSON.stringify({
        plan_only: true,
        workdir: workdir || null,
        workspace_id: workspaceId || null,
      }),
    });
    $("#programOut").textContent = JSON.stringify(out, null, 2);
  } catch (e) {
    $("#programOut").textContent = String(e);
  }
}

async function submitBindProgramStep(stepId, workdir, workspaceId) {
  try {
    if (looksAbsolutePath(workdir || "")) {
      throw new Error("Subpath/workdir must be relative (absolute paths rejected)");
    }
    const envBody = {
      plan_only: false,
      workdir: workspaceId ? (workdir || "") : (workdir || "ws"),
      workspace_id: workspaceId || null,
    };
    const env = await api(`/v1/programs/${state.programId}/steps/${stepId}/run`, {
      method: "POST",
      body: JSON.stringify(envBody),
    });
    if (!env.run_create) {
      $("#programOut").textContent = JSON.stringify(env, null, 2);
      return;
    }
    const created = await api("/v1/runs", {
      method: "POST",
      body: JSON.stringify({
        ...env.run_create,
        mode: $("#runMode")?.value || "async",
      }),
    });
    const runId = created.run_id;
    const bound = await api(`/v1/programs/${state.programId}/steps/${stepId}/run`, {
      method: "POST",
      body: JSON.stringify({
        bind_run_id: runId,
        workdir: envBody.workdir,
        workspace_id: workspaceId || null,
        idempotency_key: `bind-${runId}`,
      }),
    });
    $("#programOut").textContent = JSON.stringify({ created, bound }, null, 2);
    await openProgram(state.programId);
  } catch (e) {
    $("#programOut").textContent = String(e);
  }
}

$("#refreshPrograms").onclick = () => refreshPrograms().catch((e) => alert(e));
$("#createProgramDraft").onclick = async () => {
  try {
    const body = {
      title: "Console draft migration",
      intent: "Two-step draft from console board",
      freeze_enforcement: "advisory",
      handoff: "none",
      steps: [
        {
          step_id: "char",
          ordinal: 0,
          title: "Characterization",
          role: "characterization",
          goal: {
            desired: [{ id: "baseline", kind: "command", run: "python -m pytest -q", weight: 1.0 }],
            context: "Baseline suite",
            task_class: "repo-chore",
          },
          freeze_paths: ["src/recertia/api"],
          mutate_paths: [],
          external_handoff: { note: "operator git branch" },
        },
        {
          step_id: "move",
          ordinal: 1,
          title: "Structural move",
          role: "structural",
          goal: {
            desired: [{ id: "src-exists", kind: "file_exists", path: "src", weight: 1.0 }],
            context: "Layout change",
            task_class: "repo-chore",
          },
          freeze_paths: ["console/"],
          mutate_paths: ["src/"],
          external_handoff: { note: "operator git branch" },
        },
      ],
    };
    const created = await api("/v1/programs", { method: "POST", body: JSON.stringify(body) });
    await refreshPrograms();
    await openProgram(created.program.program_id);
  } catch (e) {
    alert(e);
  }
};
$("#acceptProgram").onclick = async () => {
  try {
    await api(`/v1/programs/${state.programId}/accept`, {
      method: "POST",
      body: JSON.stringify({ ack_disclaimer: true }),
    });
    await openProgram(state.programId);
  } catch (e) {
    alert(e);
  }
};
$("#abandonProgram").onclick = async () => {
  try {
    await api(`/v1/programs/${state.programId}/abandon`, { method: "POST", body: "{}" });
    await openProgram(state.programId);
  } catch (e) {
    alert(e);
  }
};

document.querySelector('.nav button[data-view="programs"]').addEventListener("click", () => {
  refreshPrograms().catch(() => {});
});
