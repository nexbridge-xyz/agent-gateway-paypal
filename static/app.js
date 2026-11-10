/* Agent Gateway · browser side.
 * The device key lives here: Ed25519 via WebCrypto, private key non-extractable,
 * kept in IndexedDB. The server builds every string to sign; this page signs
 * those exact bytes and shows them to the traveler first. */
"use strict";

const CATS = ["rail", "hotel", "flight", "activity", "dining", "transfer", "shopping"];
const CAT_LABEL = { rail: "Rail", hotel: "Hotel", flight: "Flight", activity: "Activity", dining: "Dining", transfer: "Transfer", shopping: "Shopping" };
const $ = (id) => document.getElementById(id);

const S = {
  status: null,
  device: null,          // { keyId, keyPair }
  deviceError: null,
  draft: null,           // { instruction, fields, source, label }
  instruction: null,
  lastEvent: 0,
  agentRunning: false,
  grid: null,
  ledgerCount: -1,
  modal: null,
  busy: false,
  keys: {},
};

/* ---------------- small helpers ---------------- */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.appendChild(typeof kid === "string" || typeof kid === "number" ? document.createTextNode(String(kid)) : kid);
  }
  return el;
}
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
function money(v) {
  const n = Number(v || 0);
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function short(id, n) { id = String(id || ""); n = n || 6; return id.length > n * 2 + 1 ? id.slice(0, n) + "…" + id.slice(-4) : id; }
function timeOf(iso) { try { return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }); } catch (e) { return ""; } }
function toast(text, kind) {
  const t = h("div", { class: "toast " + (kind || "") , text });
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), kind === "error" ? 9000 : 4500);
}
async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty */ }
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}
function b64url(bytes) {
  let s = "";
  bytes = new Uint8Array(bytes);
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
async function sha256hex(bytes) {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return Array.from(d, (b) => b.toString(16).padStart(2, "0")).join("");
}

/* ---------------- device key (WebCrypto Ed25519 + IndexedDB) ---------------- */
function idb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("agent-gateway", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("keys");
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idbGet(key) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const r = db.transaction("keys", "readonly").objectStore("keys").get(key);
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}
async function idbPut(key, value) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("keys", "readwrite");
    tx.objectStore("keys").put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}
async function setupDevice() {
  if (!window.isSecureContext || !crypto.subtle) {
    throw new Error("Signing needs a secure context. Open the app at http://localhost:8000 (or https).");
  }
  let pair = await idbGet("device").catch(() => null);
  if (!pair) {
    try {
      pair = await crypto.subtle.generateKey({ name: "Ed25519" }, false, ["sign", "verify"]);
    } catch (e) {
      throw new Error("This browser has no Ed25519 in WebCrypto. Use a current Chrome, Edge, Safari or Firefox.");
    }
    await idbPut("device", pair);
  }
  const raw = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
  const keyId = (await sha256hex(raw)).slice(0, 16);
  const res = await api("/api/device/register", { public_key: b64url(raw) });
  if (res.key_id !== keyId) throw new Error("Server computed a different key id.");
  S.device = { keyId, keyPair: pair };
}
async function signString(text) {
  const sig = await crypto.subtle.sign({ name: "Ed25519" }, S.device.keyPair.privateKey, new TextEncoder().encode(text));
  return b64url(sig);
}

/* ---------------- header ---------------- */
function renderHeader(st) {
  const pp = st.paypal;
  const bp = $("badge-paypal");
  bp.textContent = pp.live ? "PayPal Sandbox" : "PayPal simulated";
  bp.className = "badge " + (pp.live ? "live" : "sim");
  bp.title = pp.live ? "Live PayPal sandbox API (no real money)" : "Local simulator with the same interface as the PayPal API";
  const ba = $("badge-ai");
  ba.textContent = st.ai.kind === "scripted" ? "Simulated AI · scripted agent" : st.ai.label;
  ba.className = "badge " + (st.ai.kind === "scripted" ? "sim" : "ai-live");
  ba.title = st.ai.kind === "scripted" ? "No AI key: the agent's thinking is scripted; rules, signatures and ledger are real" : "Live model: " + (st.ai.model || st.ai.label);

  const done = {
    paypal: pp.status === "connected",
    mandate: !!(st.mandate && st.mandate.status === "active"),
    agent: st.carts.some((c) => c.status === "PAID" || c.status === "REFUNDED"),
  };
  let currentSet = false;
  document.querySelectorAll("#steps li").forEach((li) => {
    const k = li.dataset.step;
    li.classList.toggle("done", done[k]);
    const cur = !done[k] && !currentSet;
    li.classList.toggle("current", cur);
    if (cur) currentSet = true;
  });
}

/* ---------------- PayPal card ---------------- */
function renderPayPal(st) {
  const pp = st.paypal;
  $("paypal-mode").textContent = pp.live ? "sandbox API" : "simulated";
  const body = clear($("paypal-body"));
  if (pp.status === "connected") {
    body.appendChild(h("div", { class: "connected-line" },
      h("span", { class: "check", text: "✓ Connected" }),
      h("span", { class: "mono", text: pp.payer || "" }),
    ));
    body.appendChild(h("p", { class: "note" },
      "Vault token ", h("span", { class: "mono", text: pp.vault_token || "" }),
      " is held by the gateway and never shown to the agent. Payments run payer-not-present, inside your signed mandate."));
  } else {
    body.appendChild(h("p", { class: "lead" },
      "Save PayPal once with PayPal Vault. The gateway keeps the vault token; the agent never sees it."));
    const act = h("div", { class: "actions" },
      h("a", { class: "btn paypal", href: "/paypal/connect" }, "Connect PayPal"));
    if (!pp.live) act.appendChild(h("span", { class: "note", text: "Demo mode: a simulated approval page, no PayPal account involved." }));
    body.appendChild(act);
  }
  if (pp.last_error) body.appendChild(h("div", { class: "error-box", text: pp.last_error }));
}

/* ---------------- mandate card ---------------- */
function catChips(selected, onToggle) {
  return h("div", { class: "cat-chips" }, CATS.map((c) =>
    h("button", { type: "button", class: "cat-chip" + (selected.includes(c) ? " on" : ""), onclick: () => onToggle(c) }, CAT_LABEL[c])));
}

function renderMandate(st) {
  const body = clear($("mandate-body"));
  const tag = $("mandate-tag");
  const m = st.mandate;
  if (m && !S.draft && !S.forceNew) {
    const active = m.status === "active";
    tag.textContent = active ? "active · signed" : m.status;
    tag.className = "tag " + (active ? "ok" : "bad");
    renderActiveMandate(body, m, st);
    if (!active) {
      body.appendChild(h("div", { class: "actions" },
        h("button", { class: "btn", type: "button", onclick: () => { S.instruction = st.scenario.mandate_prompt; S.forceNew = true; renderMandate(S.status); } }, "Write a new mandate")));
    }
    return;
  }
  tag.textContent = S.draft ? "draft · not signed" : "not signed";
  tag.className = "tag";
  if (S.instruction === null) S.instruction = st.scenario.mandate_prompt;
  const ta = h("textarea", { rows: 3, maxlength: 1000, "aria-label": "What may the agent do?" });
  ta.value = S.instruction;
  ta.addEventListener("input", () => { S.instruction = ta.value; });
  body.appendChild(h("div", { class: "field full" }, h("label", { text: "What may the agent do? (plain English)" }), ta));
  const draftBtn = h("button", { class: S.draft ? "btn" : "btn primary", type: "button", onclick: () => draftMandate(draftBtn) },
    S.draft ? "Draft again with AI" : "Draft with AI");
  body.appendChild(h("div", { class: "actions" }, draftBtn,
    h("span", { class: "note", text: st.ai.kind === "scripted" ? "Scripted drafting (no AI key): plain rules read the amounts and categories." : S.draft ? "Drafted by " + st.ai.label + ". You can edit every field."
      : st.ai.label + " fills in the fields. You can edit any of them before signing." })));
  if (S.draft) renderDraftFields(body, st);
}

function renderDraftFields(body, st) {
  const f = S.draft.fields;
  const num = (key, label) => {
    const inp = h("input", { type: "text", inputmode: "decimal", value: f[key] });
    inp.addEventListener("input", () => { f[key] = inp.value; });
    return h("div", { class: "field" }, h("label", { text: label }), h("div", { class: "money" }, inp));
  };
  const maxp = h("input", { type: "number", min: 1, max: 20, value: f.max_purchases });
  maxp.addEventListener("input", () => { f.max_purchases = parseInt(maxp.value || "0", 10); });
  const until = h("input", { type: "date", value: f.valid_until });
  until.addEventListener("input", () => { f.valid_until = until.value; });
  const verified = h("input", { type: "checkbox" });
  verified.checked = !!f.verified_merchants_only;
  verified.addEventListener("change", () => { f.verified_merchants_only = verified.checked; });
  const summary = h("textarea", { rows: 2, maxlength: 240 });
  summary.value = f.summary;
  summary.addEventListener("input", () => { f.summary = summary.value; });
  const chipsWrap = h("div", {});
  const drawChips = () => {
    clear(chipsWrap).appendChild(catChips(f.allowed_categories, (c) => {
      const i = f.allowed_categories.indexOf(c);
      if (i >= 0) f.allowed_categories.splice(i, 1); else f.allowed_categories.push(c);
      drawChips();
    }));
  };
  drawChips();
  body.appendChild(h("div", { class: "fields" },
    num("total_budget", "Total budget (USD)"),
    num("auto_approve_limit", "Auto-approve up to (USD)"),
    h("div", { class: "field full" }, h("label", { text: "Allowed categories" }), chipsWrap),
    h("div", { class: "field" }, h("label", { text: "Valid until" }), until),
    h("div", { class: "field" }, h("label", { text: "Max purchases" }), maxp),
    h("div", { class: "field full" }, h("label", { class: "toggle" }, verified, "Verified merchants only")),
    h("div", { class: "field full" }, h("label", { text: "Summary you are signing" }), summary),
  ));
  const notes = f.notes || [];
  if (notes.length) {
    body.appendChild(h("div", { class: "ai-box" },
      h("div", { class: "label", text: (S.draft.source === "scripted" ? "Drafting notes" : "AI notes") + " · check before signing" }),
      h("ul", {}, notes.map((n) => h("li", { text: n })))));
  }
  const signBtn = h("button", { class: "btn primary", type: "button", onclick: () => prepareMandate(signBtn) }, "Sign on this device");
  body.appendChild(h("div", { class: "actions" }, signBtn,
    h("button", { class: "btn ghost", type: "button", onclick: () => { S.draft = null; S.forceNew = false; S.keys.mandate = null; renderMandate(S.status); } }, "Discard draft")));
}

function renderActiveMandate(body, m, st) {
  const c = m.record.constraints;
  body.appendChild(h("p", { class: "summary-line", text: m.record.summary }));
  body.appendChild(h("div", { class: "chips-line" },
    c.allowed_categories.map((x) => h("span", { class: "chip-static", text: CAT_LABEL[x] || x })),
    h("span", { class: "chip-static", text: "auto ≤ " + money(c.auto_approve_limit) }),
    h("span", { class: "chip-static", text: "until " + c.valid_until }),
    h("span", { class: "chip-static", text: "max " + c.max_purchases + " purchases" }),
    c.verified_merchants_only ? h("span", { class: "chip-static", text: "verified merchants only" }) : null,
  ));
  const b = m.budget;
  const total = Number(b.total) || 1;
  body.appendChild(h("div", { class: "budget" },
    h("div", { class: "bar" },
      h("div", { class: "paid", style: "width:" + Math.min(100, (Number(b.paid) / total) * 100) + "%" }),
      h("div", { class: "waiting", style: "width:" + Math.min(100, (Number(b.waiting) / total) * 100) + "%" })),
    h("div", { class: "legend" },
      h("span", {}, h("i", { style: "background:var(--signal)" }), "paid ", h("b", { text: money(b.paid) })),
      h("span", {}, h("i", { style: "background:var(--amber)" }), "waiting ", h("b", { text: money(b.waiting) })),
      h("span", {}, h("i", { style: "background:var(--panel-3);border:1px solid var(--line-2)" }), "left ", h("b", { text: money(b.left) })),
      h("span", {}, "of ", h("b", { text: money(b.total) })))));
  body.appendChild(h("div", { class: "sig-line" },
    h("span", {}, "device key ", h("b", { text: m.key_id })),
    h("span", {}, "signature ", h("b", { text: m.signature_fingerprint })),
    h("span", {}, "Ed25519 · ", h("b", { text: m.id }))));
  if (m.status === "active") {
    body.appendChild(h("div", { class: "actions" },
      h("button", { class: "btn danger small", type: "button", onclick: revokeMandate }, "Revoke mandate"),
      h("span", { class: "note", text: "Revoking denies every later purchase, including ones waiting for approval." })));
  } else {
    body.appendChild(h("p", { class: "revoked-banner", text: "This mandate is " + m.status + ". The gateway denies every purchase under it." }));
  }
}

async function draftMandate(btn) {
  btn.disabled = true;
  btn.textContent = "Drafting…";
  try {
    const d = await api("/api/mandate/draft", { instruction: S.instruction });
    S.draft = d;
    S.forceNew = true;
  } catch (e) { toast(e.message, "error"); }
  renderMandate(S.status);
}

async function prepareMandate(btn) {
  if (!S.device) return toast(S.deviceError || "Device key not ready.", "error");
  btn.disabled = true;
  try {
    const f = S.draft.fields;
    const p = await api("/api/mandate/prepare", { instruction: S.draft.instruction || S.instruction, fields: f, key_id: S.device.keyId });
    const r = p.record, c = r.constraints;
    openSignModal({
      title: "Sign the Intent Mandate",
      lead: "You are authorising the agent to spend inside these limits while you are not present. The gateway enforces them in code.",
      rows: [
        ["Agent", r.agent.name],
        ["Pays", r.merchant.name + " via PayPal (" + r.merchant.paypal_env + ")"],
        ["Total budget", money(c.total_budget) + " " + c.currency],
        ["Auto-approve up to", money(c.auto_approve_limit) + " (above that, you sign each cart)"],
        ["Categories", c.allowed_categories.map((x) => CAT_LABEL[x] || x).join(", ")],
        ["Valid until", c.valid_until],
        ["Max purchases", String(c.max_purchases)],
        ["Merchants", c.verified_merchants_only ? "Verified only" : "Any"],
        ["Summary", r.summary],
      ],
      signing: p.signing_string,
      keyId: p.key_id,
      button: "Sign mandate",
      onSign: async (sig) => {
        await api("/api/mandate/activate", { mandate_id: p.mandate_id, signature: sig });
        S.draft = null; S.forceNew = false; S.keys.mandate = null;
        toast("Mandate signed on this device.", "ok");
      },
    });
  } catch (e) { toast(e.message, "error"); }
  btn.disabled = false;
}

async function revokeMandate() {
  try { await api("/api/mandate/revoke", {}); toast("Mandate revoked."); } catch (e) { toast(e.message, "error"); }
  refresh();
}

/* ---------------- approvals ---------------- */
function checksList(checks) {
  return h("ul", { class: "checks" }, checks.map((c) =>
    h("li", { class: c.ok ? "" : "fail " + c.kind },
      h("span", { class: "ic", text: c.ok ? "✓" : (c.kind === "hard" ? "✕" : "!") }),
      h("span", {}, c.ok ? c.label : (c.fail_label || c.label), h("span", { class: "sub", text: " · " + c.detail })))));
}

function renderApprovals(st) {
  const body = clear($("approvals-body"));
  const pend = st.carts.filter((c) => c.status === "PENDING_APPROVAL");
  $("approvals-count").textContent = pend.length ? pend.length + " waiting" : "none";
  $("approvals-count").className = "tag" + (pend.length ? "" : "");
  if (!pend.length) {
    body.appendChild(h("p", { class: "empty", text: "Purchases above your auto-approve limit wait here for your signature. Nothing is waiting." }));
    return;
  }
  for (const c of pend) {
    body.appendChild(h("div", { class: "approval" },
      h("div", { class: "top" }, h("strong", { text: c.item.title }), h("span", { class: "price", text: c.price })),
      h("div", { class: "reason" }, h("span", { class: "mono", text: c.sku + " · " + c.item.merchant + " · " }), "Agent: “" + c.reason + "”"),
      checksList(c.decision.checks),
      h("div", { class: "actions" },
        h("button", { class: "btn primary", type: "button", onclick: (ev) => approve(c, ev.target) }, "Approve & sign"),
        h("button", { class: "btn ghost", type: "button", onclick: () => decline(c) }, "Decline"))));
  }
}

async function approve(c, btn) {
  if (!S.device) return toast(S.deviceError || "Device key not ready.", "error");
  btn.disabled = true;
  try {
    const p = await api("/api/approvals/" + c.id + "/prepare", { key_id: S.device.keyId });
    const r = p.record;
    openSignModal({
      title: "Approve and sign this cart",
      lead: "You are present and approving one purchase. The gateway re-runs the hard checks before it pays.",
      rows: [
        ["Item", r.items[0].title],
        ["When", r.items[0].detail],
        ["Merchant", r.merchant.name + (r.merchant.verified ? " (verified)" : " (not verified)")],
        ["Amount", money(r.total.value) + " " + r.total.currency],
        ["Paid to", r.payee.name + " via PayPal"],
        ["Under mandate", r.intent_mandate_id],
        ["Presence", "HUMAN_PRESENT"],
      ],
      checks: p.checks,
      signing: p.signing_string,
      keyId: p.key_id,
      button: "Approve & sign",
      onSign: async (sig) => {
        const res = await api("/api/approvals/" + c.id + "/approve", { signature: sig });
        if (res.status === "PAID") toast("Approved and paid: PayPal order " + res.paypal_order_id, "ok");
        else if (res.status === "DENIED") toast("Denied on re-check: " + res.why, "error");
        else toast(res.status + (res.error ? ": " + res.error : ""), res.error ? "error" : "");
      },
    });
  } catch (e) { toast(e.message, "error"); }
  btn.disabled = false;
}

async function decline(c) {
  try { await api("/api/approvals/" + c.id + "/decline", {}); } catch (e) { toast(e.message, "error"); }
  refresh();
}

/* ---------------- purchases ---------------- */
function renderPurchases(st) {
  const body = clear($("purchases-body"));
  const list = st.carts.filter((c) => c.status !== "PENDING_APPROVAL");
  $("purchases-count").textContent = list.length ? String(list.length) : "none";
  if (!list.length) {
    body.appendChild(h("p", { class: "empty", text: "Every purchase request appears here: paid, denied or refunded, each with an evidence bundle." }));
    return;
  }
  for (const c of list.slice().reverse()) {
    const pp = c.paypal;
    const meta = pp && pp.order_id ? "order " + pp.order_id + " · capture " + (pp.capture_id || "—")
      : (c.error ? c.error : (c.recheck && c.recheck.reason) || c.decision.reason);
    const right = h("div", { class: "r" }, h("span", { class: "amt", text: c.price }), h("span", { class: "pill " + c.status, text: c.status.replace("_", " ") }));
    const actions = h("div", { class: "r" });
    if (c.status === "PAID") actions.appendChild(h("button", { class: "btn small", type: "button", onclick: (ev) => refund(c, ev.target) }, "Refund"));
    if (c.has_evidence) actions.appendChild(h("a", { class: "btn small ghost", href: "/api/evidence/" + c.id + "?download=1", download: "evidence-" + c.id + ".json" }, "Evidence"));
    body.appendChild(h("div", { class: "purchase" },
      h("div", {}, h("div", { class: "t" }, c.item.title, c.actor !== "Tabi agent" ? h("span", { class: "sub", text: " · " + c.actor }) : null),
        h("div", { class: "m", title: meta, text: c.sku + " · " + meta })),
      h("div", { class: "purchase-side" }, right, actions)));
  }
}

async function refund(c, btn) {
  btn.disabled = true;
  try { const r = await api("/api/purchases/" + c.id + "/refund", {}); toast("Refunded through PayPal (" + (r.refund && r.refund.status) + "). Budget is back.", "ok"); }
  catch (e) { toast(e.message, "error"); }
  refresh();
}

/* ---------------- ledger (AG Grid) ---------------- */
const KIND_CLASS = {
  MANDATE_SIGNED: "ok", PAYPAL_CONNECTED: "pp", CART_DECISION: "muted", CART_SIGNED: "ok", CART_DECLINED: "muted",
  PAYPAL_CAPTURE: "pp", PAYPAL_REFUND: "pp", PAYMENT_FAILED: "bad", MANDATE_REVOKED: "bad",
};
function initGrid() {
  if (!window.agGrid) { $("ledger-grid").textContent = "AG Grid failed to load."; return; }
  const theme = agGrid.themeQuartz.withPart(agGrid.colorSchemeDark).withParams({
    backgroundColor: "#0a0c0f",
    foregroundColor: "#e7e9ec",
    headerBackgroundColor: "#161a20",
    headerTextColor: "#8e97a5",
    borderColor: "#242a33",
    rowHoverColor: "#161a20",
    selectedRowBackgroundColor: "rgba(62,242,160,0.08)",
    accentColor: "#3ef2a0",
    fontFamily: "Geist, ui-sans-serif, system-ui, sans-serif",
    fontSize: 12.5,
    headerFontSize: 12,
    rowHeight: 34,
    headerHeight: 34,
    wrapperBorderRadius: 8,
    spacing: 6,
  });
  const pill = (p) => {
    const kind = p.value || "";
    let cls = KIND_CLASS[kind] || "muted";
    if (kind === "CART_DECISION" && p.data && p.data.data) {
      cls = { APPROVED: "ok", NEEDS_APPROVAL: "wait", DENIED: "bad" }[p.data.data.result] || "muted";
    }
    return h("span", { class: "pill " + cls, text: kind.replace(/_/g, " ") });
  };
  const mono = { fontFamily: "Geist Mono, ui-monospace, monospace", fontSize: "11.5px" };
  S.grid = agGrid.createGrid($("ledger-grid"), {
    theme,
    columnDefs: [
      { field: "seq", headerName: "#", width: 58, cellStyle: mono, sort: "desc" },
      { field: "ts", headerName: "Time", width: 86, valueFormatter: (p) => timeOf(p.value), cellStyle: mono },
      { field: "kind", headerName: "Event", width: 158, cellRenderer: pill },
      { field: "summary", headerName: "Summary", flex: 1, minWidth: 160, tooltipField: "summary" },
      { headerName: "PayPal ref", width: 112, valueGetter: (p) => { const d = p.data.data || {}; return d.capture_id || d.refund_id || d.order_id || ""; }, tooltipValueGetter: (p) => p.value, cellStyle: mono },
      { field: "hash", headerName: "Hash", width: 84, valueFormatter: (p) => String(p.value || "").slice(0, 6) + "…", cellStyle: mono },
    ],
    defaultColDef: { resizable: true, sortable: true, suppressMovable: true },
    rowData: [],
    getRowId: (p) => String(p.data.seq),
    rowSelection: { mode: "singleRow", checkboxes: false, enableClickSelection: true },
    onRowClicked: (e) => {
      const pre = $("ledger-json");
      pre.textContent = JSON.stringify(e.data, null, 2);
      pre.classList.remove("hidden");
    },
  });
}
async function loadLedger(force) {
  if (!S.status) return;
  if (!force && S.status.ledger.count === S.ledgerCount) return;
  S.ledgerCount = S.status.ledger.count;
  const data = await api("/api/ledger");
  if (S.grid) S.grid.setGridOption("rowData", data.entries);
  $("verify-result").textContent = "";
  if (!data.entries.length) $("ledger-json").classList.add("hidden");
}
async function verifyChain() {
  const el = $("verify-result");
  try {
    const r = await api("/api/ledger/verify");
    el.textContent = r.ok ? "✓ " + r.count + " entries · head " + String(r.head).slice(0, 10) + "…" : "✕ broken at #" + r.broken_at + ": " + r.why;
    el.className = "verify-result " + (r.ok ? "ok" : "bad");
  } catch (e) { toast(e.message, "error"); }
}

/* ---------------- chat feed ---------------- */
let FEED_EMPTY = null;  // the "How it works" box, kept across resets
function feedAppend(el) {
  const feed = $("feed");
  FEED_EMPTY.classList.add("hidden");
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 140;
  feed.appendChild(el);
  if (nearBottom) feed.scrollTop = feed.scrollHeight;
}
function decisionCard(ev) {
  const label = { APPROVED: "APPROVED · paying", NEEDS_APPROVAL: "Waiting for your signature", DENIED: "DENIED" }[ev.result] || ev.result;
  const fails = (ev.checks || []).filter((c) => !c.ok);
  return h("div", { class: "decision " + ev.result, "data-cart": ev.cart_id },
    h("div", { class: "top" },
      h("div", {}, h("span", { class: "title", text: ev.title }), h("span", { class: "sub mono nowrap", text: " " + ev.sku })),
      h("div", { style: "display:flex;gap:10px;align-items:center" }, h("span", { class: "price", text: ev.price }),
        h("span", { class: "pill status-pill " + ev.result, text: label }))),
    h("div", { class: "meta" }, (ev.actor && ev.actor !== "Tabi agent" ? ev.actor + " · " : "") + "reason: “" + (ev.reason || "") + "”"),
    ev.result === "APPROVED"
      ? h("ul", {}, h("li", {}, h("span", { class: "ic", style: "color:var(--signal)", text: "✓" }), "All " + (ev.checks || []).length + " gateway checks passed"))
      : h("ul", {}, fails.map((c) => h("li", { class: "fail " + c.kind },
          h("span", { class: "ic", text: c.kind === "hard" ? "✕" : "!" }), (c.fail_label || c.label) + " · " + c.detail))));
}
const STATUS_LABEL = {
  PAYING: "APPROVED · paying", PAID: "PAID", CAPTURE_PENDING: "CAPTURE PENDING", PENDING_APPROVAL: "Waiting for your signature",
  DENIED: "DENIED", DECLINED: "DECLINED", REFUNDED: "REFUNDED", NOT_EXECUTED: "NOT EXECUTED",
};
// Model output stays plain text (it can quote untrusted merchant listings), so it is never
// rendered as HTML. This only removes Markdown emphasis and heading marks; line breaks are kept by CSS.
function plainText(text) {
  return String(text || "")
    .replace(/\*\*([^*\n]+)\*\*/g, "$1")
    .replace(/__([^_\n]+)__/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/`([^`\n]+)`/g, "$1");
}
function setCardStatus(cartId, status) {
  document.querySelectorAll('.decision[data-cart="' + cartId + '"] .status-pill').forEach((p) => {
    if (p.classList.contains("DENIED") && status === "DENIED") return;
    p.className = "pill status-pill " + status;
    p.textContent = STATUS_LABEL[status] || status;
  });
}
function syncCards(st) { for (const c of st.carts) setCardStatus(c.id, c.status); }
function renderEvent(ev) {
  switch (ev.kind) {
    case "user":
      feedAppend(h("div", { class: "msg user", text: ev.text }));
      break;
    case "agent_text":
      feedAppend(h("div", { class: "msg agent" + (ev.interim ? "" : " final") },
        ev.interim ? null : h("div", { class: "who", text: "Tabi" }), plainText(ev.text)));
      break;
    case "tool":
      feedAppend(h("div", { class: "row-tool" },
        ev.actor && ev.actor !== "Tabi agent" ? h("span", { class: "actor", text: ev.actor }) : null, ev.summary));
      break;
    case "decision":
      feedAppend(decisionCard(ev));
      break;
    case "receipt":
      setCardStatus(ev.cart_id, ev.capture_status === "COMPLETED" ? "PAID" : "CAPTURE_PENDING");
      feedAppend(h("div", { class: "receipt" },
        h("span", { class: "pp-tag", text: (ev.env === "sandbox" ? "PAYPAL SANDBOX" : "PAYPAL (SIM)") + " · " + (ev.capture_status || ev.order_status) }),
        h("span", { class: "ids" }, h("span", { text: "order " }), ev.order_id, h("span", { text: " · capture " }), ev.capture_id || "—"),
        h("span", { class: "amt", text: ev.price })));
      break;
    case "payment_failed":
      setCardStatus(ev.cart_id, "NOT_EXECUTED");
      feedAppend(h("div", { class: "decision DENIED" },
        h("div", { class: "top" }, h("span", { class: "title", text: "Payment not executed · " + ev.sku }), h("span", { class: "pill NOT_EXECUTED", text: "NOT EXECUTED" })),
        h("div", { class: "meta", text: ev.error })));
      break;
    case "system":
      feedAppend(h("div", { class: "row-sys", text: ev.text }));
      break;
    case "error":
      feedAppend(h("div", { class: "row-sys err", text: ev.text }));
      toast(ev.text, "error");
      break;
    case "reset":
      clear($("feed")).appendChild(FEED_EMPTY);
      FEED_EMPTY.classList.remove("hidden");
      feedAppend(h("div", { class: "row-sys", text: ev.text }));
      break;
    case "agent_status":
      setRunning(!!ev.running);
      break;
  }
}
function setRunning(on) {
  S.agentRunning = on;
  $("typing").classList.toggle("hidden", !on);
  $("btn-send").disabled = on;
  const st = $("agent-state");
  st.textContent = on ? "working" : "idle";
  st.classList.toggle("running", on);
  if (S.status) $("typing-label").textContent = S.status.ai.kind === "scripted" ? "Scripted agent is working" : S.status.ai.label + " is working";
}

/* ---------------- modal ---------------- */
function openSignModal(opts) {
  S.modal = opts;
  $("modal-title").textContent = opts.title;
  const body = clear($("modal-body"));
  body.appendChild(h("p", { class: "lead", text: opts.lead }));
  body.appendChild(h("ul", { class: "sign-list" }, opts.rows.map(([k, v]) => h("li", {}, h("span", { text: k }), h("span", { text: v })))));
  if (opts.checks) body.appendChild(checksList(opts.checks));
  $("modal-raw").textContent = opts.signing;
  $("modal-key").textContent = opts.keyId;
  $("modal-sign").textContent = opts.button;
  $("modal-sign").disabled = false;
  document.querySelector(".raw").open = false;
  $("modal").classList.remove("hidden");
  $("modal-sign").focus();
}
function closeModal() { $("modal").classList.add("hidden"); S.modal = null; }
async function modalSign() {
  const m = S.modal;
  if (!m) return;
  $("modal-sign").disabled = true;
  try {
    const sig = await signString(m.signing);
    await m.onSign(sig);
    closeModal();
  } catch (e) {
    toast(e.message, "error");
    $("modal-sign").disabled = false;
  }
  refresh();
}

/* ---------------- polling ---------------- */
async function refresh() {
  try {
    const st = await api("/api/status");
    S.status = st;
    renderHeader(st);
    // Re-render a card only when its data changed, so buttons and inputs stay put.
    const once = (name, data, fn) => {
      const key = JSON.stringify(data);
      if (S.keys[name] === key) return;
      S.keys[name] = key;
      fn(st);
    };
    once("paypal", st.paypal, renderPayPal);
    if (!S.draft && !S.forceNew) once("mandate", st.mandate, renderMandate);
    once("approvals", st.carts.filter((c) => c.status === "PENDING_APPROVAL"), renderApprovals);
    once("purchases", st.carts, renderPurchases);
    loadLedger(false);
    syncCards(st);
    if (st.agent_running !== S.agentRunning) setRunning(st.agent_running);
  } catch (e) { /* server restarting; try again */ }
}
async function pollEvents() {
  try {
    const r = await api("/api/events?after=" + S.lastEvent);
    let changed = false;
    for (const ev of r.events) {
      if (ev.seq <= S.lastEvent) continue;
      S.lastEvent = ev.seq;
      renderEvent(ev);
      changed = true;
    }
    if (changed) refresh();
  } catch (e) { /* ignore */ }
  setTimeout(pollEvents, S.agentRunning ? 700 : 1200);
}

/* ---------------- wiring ---------------- */
async function send(text) {
  text = (text || "").trim();
  if (!text) return;
  if (!S.status || !S.status.mandate || S.status.mandate.status !== "active") toast("No active mandate: the gateway will deny every purchase. Sign one first.");
  try { await api("/api/agent", { text }); setRunning(true); } catch (e) { toast(e.message, "error"); }
}

function handleReturnParams() {
  const q = new URLSearchParams(location.search);
  const p = q.get("paypal");
  if (!p) return;
  if (p === "connected") toast("PayPal connected. The vault token is held by the gateway.", "ok");
  if (p === "cancelled") toast("PayPal approval cancelled.");
  if (p === "error") toast("PayPal connection failed. See the PayPal card for details.", "error");
  history.replaceState(null, "", "/");
}

async function boot() {
  FEED_EMPTY = $("feed-empty");
  handleReturnParams();
  initGrid();
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); send($("prompt").value); });
  $("prompt").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send($("prompt").value); } });
  document.querySelectorAll(".chip[data-prompt]").forEach((b) => b.addEventListener("click", () => {
    if (!S.status) return;
    $("prompt").value = b.dataset.prompt === "agent" ? S.status.scenario.agent_prompt : S.status.scenario.followup_prompt;
    $("prompt").focus();
  }));
  $("btn-verify").addEventListener("click", verifyChain);
  $("btn-hijack").addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    try {
      const r = await api("/api/redteam/hijack", {});
      if (r.status === "DENIED") toast("Denied by the gateway. Even if the AI is fooled, the money isn't.", "ok");
      else toast("Hijack result: " + r.status, "error");
    } catch (e) { toast(e.message, "error"); }
    ev.target.disabled = false;
    refresh();
  });
  $("btn-reset").addEventListener("click", async () => {
    try { await api("/api/reset", {}); S.draft = null; S.instruction = null; S.forceNew = false; S.keys = {}; S.ledgerCount = -1; $("verify-result").textContent = ""; $("ledger-json").classList.add("hidden"); toast("Demo reset. PayPal connection kept."); }
    catch (e) { toast(e.message, "error"); }
    refresh();
  });
  $("modal-close").addEventListener("click", closeModal);
  $("modal-cancel").addEventListener("click", closeModal);
  $("modal-sign").addEventListener("click", modalSign);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && S.modal) closeModal(); });

  try { await setupDevice(); } catch (e) { S.deviceError = e.message; toast(e.message, "error"); }
  await refresh();
  if (S.status && !$("prompt").value) $("prompt").value = S.status.scenario.agent_prompt;
  // Replay the session so far, then follow new events.
  pollEvents();
  setInterval(refresh, 1500);
}

document.addEventListener("DOMContentLoaded", boot);
