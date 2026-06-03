// Self-contained dashboard page: no external scripts, styles, or fonts, so it
// works fully offline (CLAUDE.md §2). Vanilla JS fetches the read-only JSON APIs
// and renders. Kept deliberately small — it's a viewer, not an app.

// When `embed` is provided, the data is baked into the page so it renders with
// no server (used for static snapshots/exports). Otherwise the page fetches the
// read-only JSON APIs live.
export function dashboardHtml(embed?: { summary: unknown; requests: unknown }): string {
  const embedScript = embed
    ? `<script>window.__TOLLGATE_EMBED__=${JSON.stringify(embed).replace(/</g, "\\u003c")};</script>`
    : "";
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Tollgate</title>
<style>
  :root { color-scheme: light dark; --fg:#1a1a1a; --muted:#666; --line:#e2e2e2; --accent:#2d6cdf; --bg:#fafafa; --card:#fff; }
  @media (prefers-color-scheme: dark){ :root { --fg:#e6e6e6; --muted:#9aa0a6; --line:#2a2a2a; --accent:#5b8def; --bg:#161616; --card:#1f1f1f; } }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; color:var(--fg); background:var(--bg); }
  header { padding:18px 24px; border-bottom:1px solid var(--line); display:flex; align-items:baseline; gap:12px; }
  h1 { font-size:18px; margin:0; }
  .sub { color:var(--muted); font-size:12px; }
  main { padding:24px; max-width:1100px; margin:0 auto; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:24px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .card .k { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .card .v { font-size:22px; font-weight:600; margin-top:4px; }
  h2 { font-size:14px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:28px 0 10px; }
  table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line); }
  th { font-size:12px; color:var(--muted); font-weight:600; }
  tr:last-child td { border-bottom:none; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width:720px){ .grid2 { grid-template-columns:1fr; } }
  .bar { height:8px; background:var(--accent); border-radius:4px; }
  .findings { color:var(--muted); font-size:12px; }
  .tag { display:inline-block; font-size:11px; padding:1px 6px; border:1px solid var(--line); border-radius:999px; margin-right:4px; }
  .sev-high { color:#c0392b; border-color:#c0392b55; }
  .sev-warn { color:#b9770e; border-color:#b9770e55; }
  .sev-info { color:var(--muted); }
  code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
  .empty { color:var(--muted); padding:24px; text-align:center; }
  a { color:var(--accent); text-decoration:none; }
</style>
</head>
<body>
<header>
  <h1>Tollgate</h1>
  <span class="sub">local LLM spend dashboard · read-only · no data leaves this machine</span>
</header>
<main id="app"><div class="empty">Loading…</div></main>
${embedScript}
<script>
const $ = (h) => { const t=document.createElement('template'); t.innerHTML=h.trim(); return t.content.firstChild; };
const usd = (n) => '$' + (n||0).toFixed(n>=1?2:6);
const num = (n) => (n||0).toLocaleString();
const esc = (s) => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function load() {
  if (window.__TOLLGATE_EMBED__) { render(window.__TOLLGATE_EMBED__.summary, window.__TOLLGATE_EMBED__.requests); return; }
  const [summary, requests] = await Promise.all([
    fetch('/_tollgate/api/summary').then(r=>r.json()),
    fetch('/_tollgate/api/requests?limit=100').then(r=>r.json()),
  ]);
  render(summary, requests);
}

function breakdownTable(title, rows) {
  const body = rows.length ? rows.map(r => \`<tr><td>\${esc(r.key)}</td><td class="num">\${num(r.requests)}</td><td class="num">\${num(r.tokens)}</td><td class="num">\${usd(r.cost)}</td></tr>\`).join('')
    : '<tr><td colspan="4" class="empty">no data</td></tr>';
  return \`<div><h2>\${title}</h2><table><thead><tr><th>\${title.split(' ').pop()}</th><th class="num">reqs</th><th class="num">tokens</th><th class="num">cost</th></tr></thead><tbody>\${body}</tbody></table></div>\`;
}

function spendTable(buckets) {
  const max = Math.max(1, ...buckets.map(b=>b.cost));
  const body = buckets.length ? buckets.map(b => \`<tr><td>\${esc(b.day)}</td><td class="num">\${num(b.requests)}</td><td class="num">\${usd(b.cost)}</td><td style="width:40%"><div class="bar" style="width:\${Math.max(2,(b.cost/max)*100)}%"></div></td></tr>\`).join('')
    : '<tr><td colspan="4" class="empty">no data</td></tr>';
  return \`<h2>Spend over time</h2><table><thead><tr><th>day</th><th class="num">reqs</th><th class="num">cost</th><th>relative</th></tr></thead><tbody>\${body}</tbody></table>\`;
}

function findingTags(findings) {
  if (!findings || !findings.length) return '<span class="findings">—</span>';
  return findings.map(f => \`<span class="tag sev-\${f.severity}" title="\${esc(f.message)}">\${esc(f.rule)}\${f.tokensWastedEst?' ~'+num(f.tokensWastedEst):''}</span>\`).join('');
}

function requestsTable(reqs) {
  const body = reqs.length ? reqs.map(r => {
    const cost = (r.estInputCost||0)+(r.estOutputCost||0);
    const when = new Date(r.ts).toLocaleString();
    return \`<tr>
      <td><span class="findings">\${esc(when)}</span></td>
      <td>\${esc(r.provider)}/<code>\${esc(r.model)}</code></td>
      <td>\${esc(r.routeLabel||'')}</td>
      <td>\${esc(r.requestType||'—')}</td>
      <td class="num">\${r.inputTokensActual!=null?num(r.inputTokensActual):'~'+num(r.inputTokensEst)}</td>
      <td class="num">\${r.outputTokensActual!=null?num(r.outputTokensActual):'—'}</td>
      <td class="num">\${cost?usd(cost):'<span class="findings">unknown</span>'}</td>
      <td>\${findingTags(r.findings)} <a href="/_tollgate/receipt/\${encodeURIComponent(r.id)}" target="_blank">receipt</a></td>
    </tr>\`;
  }).join('') : '<tr><td colspan="8" class="empty">No requests captured yet. Point a tool at the proxy to get started.</td></tr>';
  return \`<h2>Recent requests</h2><table><thead><tr><th>time</th><th>model</th><th>route</th><th>type</th><th class="num">in</th><th class="num">out</th><th class="num">cost</th><th>findings</th></tr></thead><tbody>\${body}</tbody></table>\`;
}

function render(s, requests) {
  const t = s.summary;
  const app = document.getElementById('app');
  app.innerHTML = '';
  app.appendChild($(\`<div class="cards">
    <div class="card"><div class="k">Requests</div><div class="v">\${num(t.requests)}</div></div>
    <div class="card"><div class="k">Input tokens</div><div class="v">\${num(t.inputTokens)}</div></div>
    <div class="card"><div class="k">Output tokens</div><div class="v">\${num(t.outputTokens)}</div></div>
    <div class="card"><div class="k">Est. spend</div><div class="v">\${usd(t.cost)}</div></div>
  </div>\`));
  app.appendChild($(spendTable(s.spendOverTime)));
  app.appendChild($(\`<div class="grid2">\${breakdownTable('By model', s.byModel)}\${breakdownTable('By route', s.byRoute)}</div>\`));
  app.appendChild($(\`<div class="grid2">\${breakdownTable('By request type', s.byType)}<div></div></div>\`));
  app.appendChild($(requestsTable(requests)));
}

load().catch(e => { document.getElementById('app').innerHTML = '<div class="empty">Failed to load: '+esc(e.message)+'</div>'; });
</script>
</body>
</html>`;
}
