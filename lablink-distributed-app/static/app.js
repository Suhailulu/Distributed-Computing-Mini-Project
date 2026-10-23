const $ = id => document.getElementById(id);
const fmt = value => {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
};
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
}[c]));
async function api(path, options={}) {
  const response = await fetch(path, {
    headers: {"Content-Type":"application/json", ...(options.headers || {})},
    ...options
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}
function localInputToISO(value) {
  if (!value) return "";
  return new Date(value).toISOString();
}
function showResult(message, error=false) {
  const el = $("result");
  el.textContent = message;
  el.classList.remove("hidden", "error");
  if (error) el.classList.add("error");
}
async function loadAll() {
  try {
    const [resources, reservations, queue, events] = await Promise.all([
      api("/api/resources"), api("/api/reservations"), api("/api/queue"), api("/api/events?limit=12")
    ]);
    $("resourceCount").textContent = resources.length;
    $("reservationCount").textContent = reservations.filter(r => r.status === "CONFIRMED").length;
    $("queueCount").textContent = queue.length;
    const selected = $("resourceId").value;
    $("resourceId").innerHTML = resources.map(r =>
      `<option value="${esc(r.id)}">${esc(r.id)} — ${esc(r.name)} (${esc(r.location)})</option>`
    ).join("");
    if (resources.some(r => r.id === selected)) $("resourceId").value = selected;
    $("resources").innerHTML = resources.map(r => `
      <div class="resource"><div><b>${esc(r.name)} (${esc(r.id)})</b>
      <small>${esc(r.location)} · capacity ${r.capacity}</small></div>
      <span class="tag">${r.active ? "ACTIVE" : "INACTIVE"}</span></div>`).join("") || "<p>No resources registered.</p>";
    $("reservations").innerHTML = reservations.map(r => `
      <tr><td><b>${esc(r.resource_name)}</b><br><small>${esc(r.resource_id)}</small></td>
      <td>${esc(r.user_id)}</td><td>${fmt(r.start_time)}<br>to ${fmt(r.end_time)}</td>
      <td><span class="pill ${r.status.toLowerCase()}">${esc(r.status)}</span></td>
      <td>${r.status === "CONFIRMED" ? `<button class="small-btn" onclick="cancelBooking('${esc(r.id)}')">Cancel</button>` : "—"}</td></tr>`
    ).join("") || `<tr><td colspan="5">No reservations yet.</td></tr>`;
    $("queue").innerHTML = queue.map(q => `<div class="stack-item"><strong>${esc(q.resource_name)} · ${esc(q.user_id)}</strong>
      ${fmt(q.start_time)} → ${fmt(q.end_time)}<small>Request ${esc(q.request_id)}</small></div>`).join("") || "<p class='hint'>No requests waiting.</p>";
    $("events").innerHTML = events.map(e => `<div class="stack-item"><strong>${esc(e.event_type)}</strong>
      ${esc(e.detail)}<small>${fmt(e.created_at)} · ${esc(e.request_id || "system event")}</small></div>`).join("") || "<p class='hint'>No events recorded.</p>";
  } catch (err) {
    showResult(`Could not load data: ${err.message}`, true);
  }
}
$("bookingForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  const payload = {
    request_id: crypto.randomUUID(),
    user_id: $("userId").value.trim(),
    resource_id: $("resourceId").value,
    start_time: localInputToISO($("startTime").value),
    end_time: localInputToISO($("endTime").value)
  };
  try {
    const result = await api("/api/reservations", {method:"POST", body:JSON.stringify(payload)});
    showResult(`${result.status}: request ${result.request_id}${result.reservation_id ? ` · reservation ${result.reservation_id}` : ""}`);
    await loadAll();
  } catch (err) {
    showResult(err.message, true);
  } finally {
    button.disabled = false;
  }
});
async function cancelBooking(id) {
  if (!confirm("Cancel this reservation?")) return;
  try {
    await api(`/api/reservations/${encodeURIComponent(id)}/cancel`, {method:"POST"});
    await loadAll();
  } catch (err) {
    showResult(err.message, true);
  }
}
loadAll();
setInterval(loadAll, 15000);
