"""ui.py: presentation layer for the GoldenGate monitoring portal HTML page."""
import base64
import hashlib
import html

ATTENTION_STATUSES = ("DOWN", "STALE", "MISSING", "UNKNOWN")

OVERALL_HEALTHY = "HEALTHY"
OVERALL_ATTENTION = "ATTENTION"
OVERALL_LIMITED_VISIBILITY = "LIMITED_VISIBILITY"

_ESCAPED_DASH = html.escape("-")


def _esc(value, default="-"):
    return html.escape(default) if value is None else html.escape(str(value))


def format_relative_age(seconds, missing_text="never"):
    if seconds is None:
        return missing_text
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def format_lag_threshold_mode(lag_seconds, threshold_seconds, mode):
    if lag_seconds is None and threshold_seconds is None:
        return "N/A"
    lag_text = f"{lag_seconds}s" if lag_seconds is not None else "?"
    threshold_text = f"thr {threshold_seconds}s" if threshold_seconds is not None else "thr ?"
    mode_text = str(mode) if mode else "?"
    return f"{lag_text} / {threshold_text} ({mode_text})"


CSS_TEXT = """
:root {
  --gg-navy: #0b2545;
  --gg-navy-light: #16345f;
  --gg-blue: #0057b8;
  --gg-red: #c8102e;
  --gg-brand-red: #c8102e;
  --gg-teal: #1b998b;
  --gg-green: #1a7f37;
  --gg-green-bg: #dafbe1;
  --gg-amber: #9a6700;
  --gg-amber-bg: #fff8c5;
  --gg-red-bg: #ffebe9;
  --gg-gray: #57606a;
  --gg-gray-bg: #eaeef2;
  --gg-bg: #f6f8fa;
  --gg-surface: #ffffff;
  --gg-text: #1f2328;
  --gg-text-muted: #57606a;
  --gg-border: #d0d7de;
  --gg-focus: #0057b8;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme]) {
    --gg-navy: #16345f;
    --gg-bg: #0d1117;
    --gg-surface: #161b22;
    --gg-text: #e6edf3;
    --gg-text-muted: #9198a1;
    --gg-border: #30363d;
    --gg-green: #56d364;
    --gg-green-bg: #12261c;
    --gg-amber: #e3b341;
    --gg-amber-bg: #2b2111;
    --gg-red: #ff7b72;
    --gg-red-bg: #2d1214;
    --gg-gray: #b1bac4;
    --gg-gray-bg: #21262d;
    --gg-focus: #58a6ff;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --gg-navy: #16345f;
  --gg-bg: #0d1117;
  --gg-surface: #161b22;
  --gg-text: #e6edf3;
  --gg-text-muted: #9198a1;
  --gg-border: #30363d;
  --gg-green: #56d364;
  --gg-green-bg: #12261c;
  --gg-amber: #e3b341;
  --gg-amber-bg: #2b2111;
  --gg-red: #ff7b72;
  --gg-red-bg: #2d1214;
  --gg-gray: #b1bac4;
  --gg-gray-bg: #21262d;
  --gg-focus: #58a6ff;
  color-scheme: dark;
}
:root[data-theme="light"] {
  color-scheme: light;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--gg-bg);
  color: var(--gg-text);
  line-height: 1.45;
}
a { color: var(--gg-blue); }
:root[data-theme="dark"] a, :root[data-theme="dark"] .site-header a { color: #79c0ff; }
.skip-note { position: absolute; left: -9999px; }
.site-header {
  background: var(--gg-navy);
  color: #ffffff;
  padding: 14px 20px;
}
.site-header a { color: #ffffff; }
.header-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  max-width: 1400px;
  margin: 0 auto;
}
.brand { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.brand h1 { font-size: 1.25rem; margin: 0; font-weight: 700; }
.brand .subtitle { font-size: 0.85rem; opacity: 0.85; }
.badge-env {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  background: var(--gg-brand-red);
  color: #ffffff;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.04em;
}
.header-controls { margin-left: auto; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: rgba(255, 255, 255, 0.12);
  color: #ffffff;
  border: 1px solid rgba(255, 255, 255, 0.35);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 0.85rem;
  font-weight: 600;
  cursor: pointer;
  text-decoration: none;
}
.btn:hover { background: rgba(255, 255, 255, 0.22); }
.btn:focus-visible, a:focus-visible, button:focus-visible {
  outline: 3px solid var(--gg-focus);
  outline-offset: 2px;
}
main { max-width: 1400px; margin: 0 auto; padding: 20px; }
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}
.stat-card {
  background: var(--gg-surface);
  border: 1px solid var(--gg-border);
  border-radius: 8px;
  padding: 12px 14px;
}
.stat-card .stat-label { font-size: 0.75rem; color: var(--gg-text-muted); text-transform: uppercase; letter-spacing: 0.03em; }
.stat-card .stat-value { font-size: 1.6rem; font-weight: 700; margin-top: 2px; }
.stat-card.attention .stat-value { color: var(--gg-red); }
.stat-card.ok .stat-value { color: var(--gg-green); }
.overall-banner {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px;
  border-radius: 999px;
  font-weight: 700;
  font-size: 0.85rem;
}
.overall-banner.ok { background: var(--gg-green-bg); color: var(--gg-green); }
.overall-banner.attention { background: var(--gg-red-bg); color: var(--gg-red); }
.overall-banner.limited { background: var(--gg-amber-bg); color: var(--gg-amber); }
.refresh-info { font-size: 0.78rem; color: rgba(255, 255, 255, 0.85); }
.alert-banner {
  background: var(--gg-red-bg);
  border: 1px solid var(--gg-red);
  color: var(--gg-red);
  padding: 12px 16px;
  border-radius: 8px;
  margin-bottom: 18px;
  font-weight: 600;
}
.pipeline-section { margin-bottom: 28px; }
.pipeline-section h2 { font-size: 1.1rem; margin-bottom: 10px; }
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 16px;
}
.card {
  background: var(--gg-surface);
  border: 1px solid var(--gg-border);
  border-radius: 8px;
  padding: 14px 16px 16px;
}
.card-title-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.card-title { font-size: 1.05rem; font-weight: 700; }
.card-link-external {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border-radius: 4px;
  color: var(--gg-text-muted);
  text-decoration: none;
}
.card-link-external:hover { color: var(--gg-blue); background: var(--gg-gray-bg); }
.card-chips { margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
.field-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 6px 14px;
  margin-top: 10px;
  font-size: 0.85rem;
}
.field-label { color: var(--gg-text-muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em; }
.field-value { font-weight: 600; }
.services-row { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.chip {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-weight: 700;
  font-size: 0.78rem;
  border: 1px solid transparent;
}
.chip-up, .chip-fresh, .chip-running, .chip-reachable { background: var(--gg-green-bg); color: var(--gg-green); }
.chip-starting { background: var(--gg-amber-bg); color: var(--gg-amber); }
.chip-down, .chip-missing, .chip-abended, .chip-unreachable { background: var(--gg-red-bg); color: var(--gg-red); }
.chip-stale, .chip-stopped { background: var(--gg-amber-bg); color: var(--gg-amber); }
.chip-unknown { background: var(--gg-gray-bg); color: var(--gg-gray); }
.table-scroll { overflow-x: auto; margin-top: 12px; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
th, td { padding: 6px 10px; border-bottom: 1px solid var(--gg-border); text-align: left; vertical-align: top; }
thead th { background: var(--gg-gray-bg); position: sticky; top: 0; }
tr.stale-row td { background: var(--gg-amber-bg); }
tr.abended-row td { background: var(--gg-red-bg); }
.empty-state {
  border: 1px dashed var(--gg-border);
  border-radius: 8px;
  padding: 16px;
  margin-top: 12px;
  background: var(--gg-gray-bg);
}
.empty-state .empty-title { font-weight: 700; }
.empty-state .empty-detail { color: var(--gg-text-muted); font-size: 0.85rem; margin-top: 4px; }
footer {
  max-width: 1400px;
  margin: 20px auto 0;
  padding: 12px 20px 24px;
  color: var(--gg-text-muted);
  font-size: 0.78rem;
}
@media (prefers-reduced-motion: reduce) {
  * { animation-duration: 0.001ms !important; transition-duration: 0.001ms !important; }
}
@media (max-width: 640px) {
  .header-row { flex-direction: column; align-items: flex-start; }
  .header-controls { margin-left: 0; }
}
""".strip("\n")


JS_TEXT = """
(function () {
  var THEME_KEY = "gg-monitor-theme";
  var root = document.documentElement;
  function applyTheme(theme) {
    root.setAttribute("data-theme", theme === "dark" ? "dark" : "light");
  }
  function savedTheme() {
    try {
      return localStorage.getItem(THEME_KEY);
    } catch (err) {
      return null;
    }
  }
  function systemTheme() {
    var mql = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
    return mql && mql.matches ? "dark" : "light";
  }
  applyTheme(savedTheme() || systemTheme());
  document.addEventListener("DOMContentLoaded", function () {
    var toggle = document.getElementById("theme-toggle");
    if (!toggle) {
      return;
    }
    function currentTheme() {
      return root.getAttribute("data-theme") === "dark" ? "dark" : "light";
    }
    function refreshButton(theme) {
      var isDark = theme === "dark";
      toggle.setAttribute("aria-pressed", isDark ? "true" : "false");
      toggle.textContent = isDark ? "Light mode" : "Dark mode";
    }
    refreshButton(currentTheme());
    toggle.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (err) {
        /* localStorage unavailable -- theme still applies for this view */
      }
      refreshButton(next);
    });
  });
})();
""".strip("\n")


def _sha256_directive(text):
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


STYLE_CSP_HASH = _sha256_directive(CSS_TEXT)
SCRIPT_CSP_HASH = _sha256_directive(JS_TEXT)

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    f"style-src '{STYLE_CSP_HASH}'; "
    f"script-src '{SCRIPT_CSP_HASH}'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "object-src 'none';"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}


def _status_chip(status):
    status_text = str(status or "UNKNOWN")
    css_class = "chip-" + status_text.lower().replace("_", "-")
    return f'<span class="chip {html.escape(css_class)}">{html.escape(status_text)}</span>'


def _fresh_chip(fresh):
    label = "Fresh" if fresh else "STALE"
    css_class = "chip-fresh" if fresh else "chip-stale"
    return f'<span class="chip {css_class}">{html.escape(label)}</span>'


def _reachable_chip(reachable):
    label = "reachable" if reachable else "down"
    css_class = "chip-reachable" if reachable else "chip-unreachable"
    return f'<span class="chip {css_class}">{html.escape(label)}</span>'


_DISCOVERY_STATUS_TEXT = {
    "OK": "Complete",
    "EMPTY": "Empty inventory",
    "PARTIAL": "Partially available",
    "UNAVAILABLE": "Unavailable",
    "INVALID_RESPONSE": "Invalid response",
}

_DISCOVERY_EXPLANATION = {
    "EMPTY": ("No replication processes discovered",
             "The GoldenGate Admin REST API returned a valid empty Extract and Replicat inventory. "
             "Replication health is not claimed."),
    "PARTIAL": ("Process discovery partially available",
               "Only part of the GoldenGate process inventory was available during the latest monitoring cycle."),
    "UNAVAILABLE": ("Process discovery unavailable",
                    "The monitor could not retrieve the Extract and Replicat inventories during the latest cycle."),
    "INVALID_RESPONSE": ("Invalid process inventory response",
                         "The latest Admin REST inventory response did not match the approved structure."),
}

_DISCOVERY_ATTENTION_STATUSES = ("PARTIAL", "UNAVAILABLE", "INVALID_RESPONSE")


def _discovery_status_text(discovery):
    if not discovery:
        return "Not reported"
    return _DISCOVERY_STATUS_TEXT.get(str(discovery.get("status") or ""), "Not reported")


def _discovery_counts_text(discovery):
    discovery = discovery or {}
    return (f"Extract {_esc(discovery.get('extractCount', 0))} · "
           f"Replicat {_esc(discovery.get('replicatCount', 0))} · "
           f"Distribution {_esc(discovery.get('distpathCount', 0))}")


def _render_discovery_field(discovery):
    status_label = html.escape(_discovery_status_text(discovery))
    return _field("Process discovery", f"{status_label} &middot; {_discovery_counts_text(discovery)}")


def _render_discovery_explanation(discovery):
    status = str((discovery or {}).get("status") or "")
    info = _DISCOVERY_EXPLANATION.get(status)
    if not info:
        return ""
    title, detail = info
    return (
        '<div class="empty-state">'
        f'<div class="empty-title">{html.escape(title)}</div>'
        f'<div class="empty-detail">{html.escape(detail)}</div>'
        "</div>")


def _critical_services_html(critical_services):
    if not critical_services:
        return '<span class="field-value">-</span>'
    return " ".join(
        f"{_esc(svc)} {_reachable_chip(up is True)}"
        for svc, up in sorted(critical_services.items()))


def _alerts_enabled_text(value):
    if value is None:
        return "unknown"
    return "true" if value else "false"


def _field(label, value_html):
    return (f'<div class="field"><div class="field-label">{html.escape(label)}</div>'
            f'<div class="field-value">{value_html}</div></div>')


# Minimal inline "open in new tab" SVG (no new frontend dependency) -- aria-hidden since the surrounding <a> itself already carries the accessible name via aria-label.
_EXTERNAL_LINK_ICON_SVG = (
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
    'focusable="false"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6">'
    '</path><polyline points="15 3 21 3 21 9"></polyline>'
    '<line x1="10" y1="14" x2="21" y2="3"></line></svg>'
)


def _runtime_console_link_html(deployment_name, console_url):
    """The per-card "Open GoldenGate UI" external-link action -- rendered only when the canonical topology already provides a real, https-scheme runtime URL (config.load_deployments() returns None whenever ingress is disabled or the host is invalid, and never fabricates one); the scheme is re-checked here too, defensively, so a rendered href can never become a javascript:/data:/other executable scheme even if this function is ever called with an unexpected value. The card itself never becomes a link -- only this small icon is clickable."""
    if not (isinstance(console_url, str) and console_url.startswith("https://")):
        return ""
    escaped_url = html.escape(console_url, quote=True)
    escaped_label = html.escape(f"Open GoldenGate UI for {deployment_name}", quote=True)
    return (
        f'<a class="card-link-external" href="{escaped_url}" target="_blank" '
        f'rel="noopener noreferrer" aria-label="{escaped_label}" title="Open GoldenGate UI">'
        f'{_EXTERNAL_LINK_ICON_SVG}</a>'
    )


def _render_process_section(processes, discovery):
    """Empty processes with a reported discovery status defer to that status's own explanation block."""
    if not processes and discovery:
        return ""
    return _render_process_table(processes)


def _render_process_table(processes):
    if not processes:
        return (
            '<div class="empty-state">'
            '<div class="empty-title">No process state available</div>'
            '<div class="empty-detail">No Extract or Replicat process STATE rows have been recorded.</div>'
            "</div>")
    rows = []
    for p in processes:
        stale = bool(p.get("stale"))
        abended = str(p.get("status")) == "ABENDED"
        process_cell = _esc(p.get("process"))
        if stale:
            process_cell = f'<strong>[STALE]</strong> {process_cell}'
        lag_cell = html.escape(format_lag_threshold_mode(
            p.get("lagSeconds"), p.get("resolvedThreshold"), p.get("resolvedMode")))
        age_cell = html.escape(format_relative_age(p.get("ageSeconds")))
        error_cell = _esc(p.get("statusMessage")) if p.get("hasError") else ""
        row_classes = " ".join(c for c in ("stale-row" if stale else "", "abended-row" if abended else "") if c)
        row_open = f'<tr class="{row_classes}">' if row_classes else "<tr>"
        rows.append(
            f"{row_open}"
            f"<td>{process_cell}</td>"
            f"<td>{_esc(p.get('processType'))}</td>"
            f"<td>{_status_chip(p.get('status'))}</td>"
            f"<td>{lag_cell}</td>"
            f"<td>{age_cell}</td>"
            f"<td>{_esc(p.get('consecutiveAbends'))}</td>"
            f"<td>{error_cell}</td>"
            "</tr>")
    return (
        '<div class="table-scroll"><table>'
        "<thead><tr><th>Process</th><th>Type</th><th>Status</th>"
        "<th>Lag / Threshold (mode)</th><th>Recorded</th><th>Abends</th><th>Error</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>")


def _render_deployment_card(r):
    fresh = bool(r.get("fresh"))
    age_text = html.escape(format_relative_age(r.get("ageSeconds"), missing_text="-"))
    process_count = len(r.get("processes") or [])

    lease = r.get("lease")
    if lease:
        holder_text = _esc(lease.get("holder") or "none")
        state_text = "valid" if lease.get("fresh") else "EXPIRED"
        lease_holder_html = holder_text
        lease_state_html = html.escape(state_text)
    else:
        lease_holder_html = "none"
        lease_state_html = "-"

    fields = "".join([
        _field("Role", _esc(r.get("role"))),
        _field("Deployment type", _esc(r.get("deploymentType"))),
        _field("Data source", _esc(r.get("dataSource"))),
        _field("Last updated", age_text),
        _field("Lease holder", lease_holder_html),
        _field("Lease validity", lease_state_html),
        _field("Alerts enabled", html.escape(_alerts_enabled_text(r.get("alertsEnabled")))),
        _field("Metrics enabled", html.escape(_alerts_enabled_text(r.get("metricsEnabled")))),
        _field("Process count", str(process_count)),
        _render_discovery_field(r.get("processDiscovery")),
    ])

    chips = f'{_status_chip(r.get("effectiveStatus"))}{_fresh_chip(fresh)}'
    services_html = _critical_services_html(r.get("criticalServices"))
    discovery_explanation_html = _render_discovery_explanation(r.get("processDiscovery"))
    console_link_html = _runtime_console_link_html(r.get("deploymentName"), r.get("consoleUrl"))

    return (
        '<article class="card">'
        '<div class="card-title-row">'
        f'<div class="card-title">{_esc(r.get("deploymentName"))}</div>'
        f'{console_link_html}'
        '</div>'
        f'<div class="card-chips">{chips}</div>'
        f'<div class="field-grid">{fields}</div>'
        f'<div class="services-row"><span class="field-label">Critical services</span> {services_html}</div>'
        f'{discovery_explanation_html}'
        f'{_render_process_section(r.get("processes") or [], r.get("processDiscovery"))}'
        "</article>")


def _compute_overall_state(total_deployments, attention_deployments, services_down, abended_processes,
                           stale_processes, discovery_issues, all_deployments_current_ok):
    """HEALTHY requires EVERY deployment individually current -- one healthy runtime never hides another's gap."""
    if total_deployments == 0:
        return OVERALL_ATTENTION
    if (attention_deployments > 0 or services_down > 0 or abended_processes > 0
            or stale_processes > 0 or discovery_issues > 0):
        return OVERALL_ATTENTION
    if not all_deployments_current_ok:
        return OVERALL_LIMITED_VISIBILITY
    return OVERALL_HEALTHY


def _compute_summary(payload):
    total_deployments = 0
    up_deployments = 0
    attention_deployments = 0
    reachable_services = 0
    total_services = 0
    total_processes = 0
    abended_processes = 0
    stale_processes = 0
    active_processes = 0
    discovery_issues = 0
    any_processes = False
    all_deployments_current_ok = True

    for lp in payload.get("logicalPipelines", []):
        for r in lp.get("runtimes", []):
            total_deployments += 1
            status = r.get("effectiveStatus")
            if status == "UP":
                up_deployments += 1
            if status in ATTENTION_STATUSES:
                attention_deployments += 1
            services = r.get("criticalServices") or {}
            total_services += len(services)
            reachable_services += sum(1 for v in services.values() if v is True)
            processes = r.get("processes") or []
            if processes:
                any_processes = True
            total_processes += len(processes)
            abended_processes += sum(1 for p in processes if p.get("status") == "ABENDED")
            stale_processes += sum(1 for p in processes if p.get("stale"))
            own_active_processes = sum(1 for p in processes if not p.get("stale"))
            active_processes += own_active_processes
            discovery_status = (r.get("processDiscovery") or {}).get("status")
            if discovery_status in _DISCOVERY_ATTENTION_STATUSES:
                discovery_issues += 1
            if not (discovery_status == "OK" and own_active_processes > 0):
                all_deployments_current_ok = False

    services_down = total_services - reachable_services
    overall_state = _compute_overall_state(
        total_deployments, attention_deployments, services_down, abended_processes,
        stale_processes, discovery_issues, all_deployments_current_ok)

    return {
        "totalDeployments": total_deployments,
        "upDeployments": up_deployments,
        "attentionDeployments": attention_deployments,
        "reachableServices": reachable_services,
        "totalServices": total_services,
        "servicesDown": services_down,
        "totalProcesses": total_processes,
        "abendProcesses": abended_processes,
        "staleProcesses": stale_processes,
        "discoveryIssues": discovery_issues,
        "anyProcesses": any_processes,
        "overallState": overall_state,
    }


def _render_summary(summary):
    stat_cards = [
        ("Deployments", str(summary["totalDeployments"]), ""),
        ("Up", str(summary["upDeployments"]), "ok"),
        ("Needs attention", str(summary["attentionDeployments"]),
         "attention" if summary["attentionDeployments"] else "ok"),
        ("Services reachable", f'{summary["reachableServices"]}/{summary["totalServices"]}', ""),
        ("Processes discovered", str(summary["totalProcesses"]), ""),
    ]
    if summary["anyProcesses"]:
        stat_cards.append(("Process ABENDs", str(summary["abendProcesses"]),
                           "attention" if summary["abendProcesses"] else "ok"))
        stat_cards.append(("Stale processes", str(summary["staleProcesses"]),
                           "attention" if summary["staleProcesses"] else "ok"))
    if summary["totalDeployments"]:
        stat_cards.append(("Discovery issues", str(summary["discoveryIssues"]),
                           "attention" if summary["discoveryIssues"] else "ok"))

    cards_html = "".join(
        f'<div class="stat-card {css_class}"><div class="stat-label">{html.escape(label)}</div>'
        f'<div class="stat-value">{html.escape(value)}</div></div>'
        for label, value, css_class in stat_cards)
    return f'<div class="summary-grid">{cards_html}</div>'


def _resolve_environment_badge_text(config, payload, environment):
    value = environment or (payload or {}).get("environment") or getattr(config, "environment", None)
    if not value:
        return "ENVIRONMENT UNKNOWN"
    return html.escape(str(value).upper())


def _render_header(summary, config, payload, generated_at_text, environment):
    state = summary["overallState"]
    if state == OVERALL_HEALTHY:
        banner = '<span class="overall-banner ok">Overall: Healthy</span>'
    elif state == OVERALL_LIMITED_VISIBILITY:
        banner = '<span class="overall-banner limited">Runtime services healthy &middot; Process visibility unavailable</span>'
    else:
        banner = '<span class="overall-banner attention">Overall: Needs attention</span>'

    env_badge_text = _resolve_environment_badge_text(config, payload, environment)
    refresh_seconds = int(config.refresh_seconds)
    return f"""<header class="site-header">
<div class="header-row">
<div class="brand">
<h1>GoldenGate Monitoring</h1>
<span class="badge-env">{env_badge_text}</span>
<span class="subtitle">Oracle GoldenGate on EKS</span>
</div>
{banner}
<div class="header-controls">
<span class="refresh-info">Auto-refresh: {refresh_seconds}s &middot; Last rendered: {html.escape(generated_at_text)}</span>
<a class="btn" href="/" aria-label="Refresh page now">Refresh now</a>
<button type="button" id="theme-toggle" class="btn" aria-pressed="false">Dark mode</button>
</div>
</div>
</header>"""


def render_html(payload, config, error_message=None, environment=None):
    summary = _compute_summary(payload)
    generated_at_raw = payload.get("generatedAt", "-")
    generated_at_text = str(generated_at_raw)

    sections = []
    if error_message:
        sections.append(
            '<div class="alert-banner" role="alert">'
            f"Unable to read monitoring data: {html.escape(error_message)}</div>")

    sections.append(_render_summary(summary))

    for lp in payload.get("logicalPipelines", []):
        cards = "".join(_render_deployment_card(r) for r in lp.get("runtimes", []))
        sections.append(
            f'<section class="pipeline-section">'
            f'<h2>{_esc(lp.get("pipelineId"))}</h2>'
            f'<div class="card-grid">{cards}</div>'
            "</section>")

    if not payload.get("logicalPipelines"):
        sections.append("<p>No logical pipelines found in the canonical topology.</p>")

    stale_after = html.escape(str(config.stale_after_seconds))
    version = html.escape(str(config.monitor_version))
    refresh_seconds = int(config.refresh_seconds)
    header_html = _render_header(summary, config, payload, generated_at_text, environment)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{refresh_seconds}">
<title>GoldenGate Monitoring</title>
<script>{JS_TEXT}</script>
<style>{CSS_TEXT}</style>
</head>
<body>
{header_html}
<main>
{"".join(sections)}
</main>
<footer>Generated at {html.escape(generated_at_text)} (epoch seconds) &middot; stale after {stale_after}s &middot; monitor {version} &middot; auto-refreshes every {refresh_seconds}s</footer>
</body>
</html>
"""
