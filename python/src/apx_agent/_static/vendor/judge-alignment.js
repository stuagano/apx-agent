// Judge alignment on the eval page (#1052).
// Extracted from the eval page's inline script. Loaded after eval-landing.js,
// which defines esc().

// ── Judge Alignment ──────────────────────────────────────────────────────────
function renderAlignHistory(runs) {
  const el = document.getElementById('la-history');
  if (!el) return;
  if (!runs || !runs.length) {
    el.innerHTML = '<div style="font-size:12px;color:var(--muted)">No alignments yet.</div>';
    return;
  }
  const n = runs.length;
  el.innerHTML =
    `<div style="font-size:12px;color:var(--muted);margin:0 0 8px">Judge aligned ${n} time${n === 1 ? '' : 's'}</div>` +
    runs.map((run) => {
      const when = run.start_time ? esc(String(run.start_time).replace('T', ' ').slice(0, 19)) : 'unknown time';
      const judge = esc(run.judge_name || '');
      const gs = (run.guidelines || []).map((g, i) => `${i + 1}. ${esc(g)}`).join('<br>');
      const empty = gs ? `<small style="color:var(--apx-muted)">${gs}</small>` : '<small style="color:var(--apx-muted)">No guidelines stored.</small>';
      return `<div style="border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-bottom:6px;background:var(--panel)">
        <div style="font-size:11px;color:var(--muted);margin-bottom:4px">${when}${judge ? ' · ' + judge : ''} · ${run.trace_count || 0} traces</div>
        ${empty}
      </div>`;
    }).join('');
}

async function loadAlignHistory() {
  const el = document.getElementById('la-history');
  const judge = document.getElementById('la-judge') ? document.getElementById('la-judge').value.trim() : '';
  const qs = judge ? ('?judge_name=' + encodeURIComponent(judge)) : '';
  try {
    const r = await fetch('/_apx/eval/label-history' + qs);
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || r.statusText);
    renderAlignHistory(d.runs || []);
  } catch (e) {
    if (el) el.innerHTML = '<div style="font-size:12px;color:var(--apx-muted)">Could not load alignment history: ' + esc(e.message) + '</div>';
  }
}

async function labelAlign() {
  const judge = document.getElementById('la-judge').value.trim();
  if (!judge) { document.getElementById('la-status').textContent = 'Enter a judge name (e.g. quality).'; return; }
  document.getElementById('la-align-btn').disabled = true;
  document.getElementById('la-status').textContent = 'Reading your ratings and running MemAlign… (may take a minute)';
  try {
    const r = await fetch('/_apx/eval/label-align', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({judge_name: judge}),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    const gs = (d.guidelines || []).map((g, i) => `${i+1}. ${esc(g)}`).join('<br>');
    document.getElementById('la-status').innerHTML =
      `✓ Aligned on ${d.trace_count} traces — <strong>${(d.guidelines || []).length} guidelines distilled</strong><br><small style="color:var(--apx-muted)">${gs}</small>`;
    await loadAlignHistory();
  } catch(e) {
    document.getElementById('la-status').textContent = 'Error: ' + e.message;
  } finally {
    document.getElementById('la-align-btn').disabled = false;
  }
}
document.getElementById('la-judge').addEventListener('change', loadAlignHistory);
loadAlignHistory();
