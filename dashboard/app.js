// The rate desk. Plain JavaScript: four GET calls on load, one POST per
// decision. No framework, so the page is easy to host next to the API.

const rupees = (n) => (n == null ? "–" : "₹" + Number(n).toLocaleString("en-IN"));
const pct = (x, digits = 0) => (x == null ? "–" : (x * 100).toFixed(digits) + "%");
const $ = (sel) => document.querySelector(sel);
let days = [];
let selected = null;

async function get(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

const shortDate = (iso) => new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" });

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(c ?? ""));
  return node;
}

function kpi(label, value, note) {
  return el("div", { class: "kpi" }, el("div", { class: "value" }, value), el("div", { class: "label" }, label),
    el("div", { class: "note" }, note));
}

async function loadOverview() {
  const o = await get("/api/overview");
  $("#hotel").textContent = o.hotel.name;
  const d = new Date(o.as_of + "T00:00:00");
  $("#asof").textContent = `Business day ${d.toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long", year: "numeric" })}, ${o.hotel.rooms} rooms`;
  const last = o.last_28_days, next = o.next_30_days_on_books;
  $("#kpis").replaceChildren(
    kpi("Occupancy, last 28 nights", pct(last.occupancy), `Next 30 on the books: ${pct(next.occupancy)}`),
    kpi("ADR, last 28 nights", rupees(last.adr), `Booked ahead at ${rupees(next.adr)}`),
    kpi("RevPAR, last 28 nights", rupees(last.revpar), "Room revenue per available room"),
    kpi("Pickup, last 7 days", `${o.pickup_7_days} nights`, "Net of cancellations"),
    kpi("Pace vs 4 weeks ago", o.pace_vs_4_weeks_ago == null ? "–" : (o.pace_vs_4_weeks_ago > 0 ? "+" : "") + pct(o.pace_vs_4_weeks_ago), "Rooms on the books, same days out"),
  );
}

function drawCalendar() {
  const rates = days.flatMap((d) => [d.current_rate, d.recommended_rate, d.comp_low, d.comp_high]).filter(Boolean);
  const lo = Math.min(...rates) * 0.95, hi = Math.max(...rates) * 1.03;
  const y = (v) => 100 - ((v - lo) / (hi - lo)) * 100;
  const cal = $("#calendar");
  cal.replaceChildren(...days.map((d, i) => {
    const price = el("div", { class: "price" });
    if (d.comp_low && d.comp_high) {
      const band = el("span", { class: "band" });
      band.style.top = y(d.comp_high) + "%";
      band.style.height = Math.max(2, y(d.comp_low) - y(d.comp_high)) + "%";
      price.append(band);
    }
    const now = el("span", { class: "now" });
    now.style.top = y(d.current_rate) + "%";
    price.append(now);
    if (d.recommended_rate && d.recommended_rate !== d.current_rate) {
      const rec = el("span", { class: "rec" });
      rec.style.top = y(d.recommended_rate) + "%";
      price.append(rec);
    }
    const occ = el("div", { class: "occ" });
    const fc = el("span", { class: "fc" });
    fc.style.height = Math.min(100, ((d.forecast_rooms ?? d.on_books) / 120) * 100) + "%";
    const otb = el("span", { class: "otb" });
    otb.style.height = d.occupancy_on_books * 100 + "%";
    occ.append(fc, otb);
    const date = new Date(d.date + "T00:00:00");
    const weekend = date.getDay() === 0 || date.getDay() === 6;
    const label = `${d.weekday} ${date.getDate()} ${date.toLocaleString("en-IN", { month: "short" })}: ${d.on_books} rooms booked, rate ${rupees(d.current_rate)}`;
    const btn = el("button", { class: "day" + (weekend ? " weekend" : ""), role: "option", "aria-selected": String(i === selected), "aria-label": label, title: label, onclick: () => select(i) },
      el("div", { class: "mark" }, d.status === "pending" ? el("i", { title: "waiting for approval" }) : ""),
      price, occ, el("div", { class: "label" }, d.weekday.slice(0, 2), el("br"), String(date.getDate())));
    return btn;
  }));
}

function select(i) {
  selected = i;
  drawCalendar();
  const d = days[i];
  const box = $("#detail");
  const change = d.recommended_rate ? d.recommended_rate / d.current_rate - 1 : 0;
  const date = new Date(d.date + "T00:00:00").toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long" });
  const fc = d.reasons.find((r) => r.code === "forecast") || {};
  const facts = el("ul", { class: "facts" },
    el("li", {}, "Rooms on the books", el("b", {}, `${d.on_books} of 120`)),
    el("li", {}, "Forecast by arrival", el("b", {}, d.forecast_rooms == null ? "–" : `${d.forecast_rooms} rooms`)),
    el("li", {}, "Expected pickup at today's rate", el("b", {}, fc.pickup == null ? "–" : `${fc.pickup} rooms`)),
    el("li", {}, "Competitor median", el("b", {}, rupees(d.comp_median))),
    el("li", {}, "Competitor range", el("b", {}, d.comp_low ? `${rupees(d.comp_low)} to ${rupees(d.comp_high)}` : "–")),
  );
  const parts = [el("h3", {}, date)];
  if (d.event) parts.push(el("span", { class: "event" }, d.event));
  parts.push(el("div", { class: "rates" },
    el("div", {}, el("div", { class: "k" }, "Selling at"), el("div", { class: "v" }, rupees(d.current_rate))),
    el("div", { class: change > 0 ? "up" : change < 0 ? "down" : "" }, el("div", { class: "k" }, "Engine suggests"),
      el("div", { class: "v" }, rupees(d.recommended_rate)), el("div", { class: "k" }, change ? `${change > 0 ? "+" : ""}${pct(change, 1)}` : "no change"))));
  if (d.explanation) parts.push(el("p", { class: "why" }, d.explanation));
  parts.push(facts);
  if (d.status === "pending") {
    const input = el("input", { type: "number", step: "50", value: d.recommended_rate, "aria-label": "Rate to send" });
    const status = el("span", { class: "status" });
    const approve = el("button", { class: "btn brass", onclick: () => decide(d, "approve", Number(input.value), status, approve) }, "Send to channel manager");
    const reject = el("button", { class: "btn ghost", onclick: () => decide(d, "reject", null, status, approve) }, "Keep current rate");
    parts.push(el("div", { class: "actions" }, input, approve, reject), status);
  } else if (d.status) {
    parts.push(el("p", { class: "status" }, `Status: ${d.status.replace("_", " ")}`));
  }
  box.replaceChildren(...parts);
}

function token() {
  let t = sessionStorage.getItem("occ-token");
  if (t) return Promise.resolve(t);
  const dlg = $("#token-dialog");
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      t = $("#token-input").value.trim();
      if (dlg.returnValue === "ok" && t) {
        sessionStorage.setItem("occ-token", t);
        resolve(t);
      } else resolve(null);
    }, { once: true });
    dlg.showModal();
  });
}

async function decide(d, decision, rate, status, button) {
  const t = await token();
  if (!t) return;
  button.disabled = true;
  status.textContent = decision === "approve" ? "Sending to the channel manager" : "Saving";
  const r = await fetch(`/api/recommendations/${d.id}/decision`, {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${t}` },
    body: JSON.stringify({ decision, rate }),
  });
  const body = await r.json().catch(() => ({}));
  if (r.status === 401) sessionStorage.removeItem("occ-token");
  if (!r.ok) {
    status.textContent = r.status === 401 ? "That token was not accepted." : `Not sent: ${body.error || r.status}`;
    button.disabled = false;
    return;
  }
  status.textContent = body.status === "pushed" ? `Live on all channels (confirmation ${body.confirmation}).` : "Kept the current rate.";
  await loadCalendar();
  select(days.findIndex((x) => x.date === d.date));
}

async function loadCalendar() {
  days = (await get("/api/calendar")).days;
  drawCalendar();
  const pending = days.filter((d) => d.status === "pending")
    .sort((a, b) => Math.abs(b.recommended_rate / b.current_rate - 1) - Math.abs(a.recommended_rate / a.current_rate - 1));
  $("#queue-count").textContent = pending.length ? `${pending.length} nights, biggest moves first` : "Nothing waiting";
  $("#queue tbody").replaceChildren(...pending.slice(0, 12).map((d) => {
    const ch = d.recommended_rate / d.current_rate - 1;
    return el("tr", { class: "clickable", onclick: () => select(days.indexOf(d)) },
      el("td", {}, `${d.weekday} ${d.date.slice(8)}/${d.date.slice(5, 7)}`, d.event ? el("span", { class: "tag warn" }, " event") : ""),
      el("td", { class: "num" }, rupees(d.current_rate)), el("td", { class: "num" }, rupees(d.recommended_rate)),
      el("td", { class: "num " + (ch > 0 ? "up" : "down") }, (ch > 0 ? "+" : "") + pct(ch, 1)), el("td", {}, "Review"));
  }));
}

async function loadFeeds() {
  const { checks } = await get("/api/feeds");
  const bad = checks.filter((c) => c.status !== "pass");
  const pill = $("#health-pill");
  pill.textContent = checks.length ? `${checks.length - bad.length} of ${checks.length} data checks pass` : "No checks yet";
  pill.className = "health" + (bad.some((c) => c.status === "fail") ? " fail" : bad.length ? " warn" : "");
  pill.onclick = () => $("#feeds").scrollIntoView({ behavior: "smooth" });
  $("#checks tbody").replaceChildren(...checks.map((c) => el("tr", {},
    el("td", {}, c.feed), el("td", {}, c.check_name.replaceAll("_", " ")),
    el("td", { class: "nowrap" }, el("span", { class: `tag ${c.status}` }, c.status === "pass" ? "ok" : c.status), " ", c.value || ""),
    el("td", { class: "detail-text" }, c.detail || ""))));
}

async function loadRequests() {
  const { requests } = await get("/api/requests");
  $("#requests tbody").replaceChildren(...requests.slice(0, 10).map((q) => {
    const f = q.fields || {};
    return el("tr", { title: q.text },
      el("td", {}, f.company || el("span", { class: "tag fail" }, "unread")),
      el("td", {}, f.check_in ? `${shortDate(f.check_in)} to ${shortDate(f.check_out)}, ${f.room_type} ${f.meal_plan}` : "–"),
      el("td", { class: "num" }, rupees(f.rate)), el("td", {}, f.rooms ?? "–"),
      el("td", {}, el("span", { class: `tag ${q.valid ? "pass" : "warn"}` }, q.extractor === "slm" ? "model" : q.extractor || "–")));
  }));
}

async function loadRisk() {
  const r = await get("/api/at-risk");
  $("#risk-sum").textContent = `About ${Math.round(r.expected_cancelled_room_nights)} of the room nights arriving in the next 14 days are likely to cancel`;
  $("#risk tbody").replaceChildren(...r.bookings.slice(0, 10).map((b) => el("tr", {},
    el("td", {}, b.id), el("td", {}, `${shortDate(b.arrival)}, ${b.nights} ${b.nights === 1 ? "night" : "nights"}`), el("td", {}, b.channel),
    el("td", { class: "num" }, pct(b.risk)))));
}

async function main() {
  const tasks = [loadOverview(), loadCalendar(), loadFeeds(), loadRequests(), loadRisk()];
  const results = await Promise.allSettled(tasks);
  const failed = results.filter((r) => r.status === "rejected");
  if (failed.length) {
    $("#asof").textContent = "Some panels did not load: " + failed.map((f) => f.reason.message).join("; ");
  }
}

main();
