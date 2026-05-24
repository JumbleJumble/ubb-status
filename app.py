#!/usr/bin/env python3
import os
import subprocess
from flask import Flask, jsonify

app = Flask(__name__)

SSH_KEY = os.path.expanduser(os.environ.get("SSH_KEY_PATH", "/run/secrets/ubb_key"))
SSH_USER = "jumble.jumble"
SSH_OPTS = [
    "-i", SSH_KEY,
    "-o", "StrictHostKeyChecking=no",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o", "ConnectTimeout=5",
    "-o", "BatchMode=yes",
]

DEVICES = [
    {"name": "Near-end", "host": "192.168.4.37"},
    {"name": "Far-end",  "host": "192.168.4.38"},
]

SIGNAL_WARN  = -65
SIGNAL_ERROR = -80


def check_device(name, host):
    try:
        r = subprocess.run(
            ["ssh", *SSH_OPTS, f"{SSH_USER}@{host}", "mca-status"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {"name": name, "host": host, "reachable": False, "error": "Connection timed out"}

    if r.returncode != 0:
        return {"name": name, "host": host, "reachable": False, "error": "SSH failed"}

    # Parse key=value (first line may have comma-separated device info)
    data = {}
    for line in r.stdout.strip().splitlines():
        for part in line.split(","):
            if "=" in part:
                k, _, v = part.partition("=")
                data[k.strip()] = v.strip()

    def num(key, default=None):
        try:
            return float(data[key])
        except (KeyError, ValueError):
            return default

    signal      = num("signal")
    noise       = num("noise")
    tx_rate     = num("wlanTxRate")
    rx_rate     = num("wlanRxRate")
    uptime      = num("uptime")
    connected   = num("wlanConnections", 0)
    lan_plugged = num("lanPlugged", 1)
    tx_errors   = num("wlanTxErrors", 0)
    rx_errors   = num("wlanRxErrors", 0)
    cpu         = num("cpuUsage")
    mem_total   = num("memTotal")
    mem_free    = num("memFree")
    lan_speed   = data.get("lanSpeed", "unknown")
    firmware    = data.get("firmwareVersion", "unknown")

    problems = []
    if connected == 0:
        problems.append("No wireless link")
    if signal is not None:
        if signal < SIGNAL_ERROR:
            problems.append(f"Signal critically weak ({signal:.0f} dBm)")
        elif signal < SIGNAL_WARN:
            problems.append(f"Signal degraded ({signal:.0f} dBm)")
    if lan_plugged == 0:
        problems.append("LAN cable unplugged")
    if tx_errors and tx_errors > 100:
        problems.append(f"High TX errors ({tx_errors:.0f})")
    if rx_errors and rx_errors > 100:
        problems.append(f"High RX errors ({rx_errors:.0f})")

    snr = (signal - noise) if (signal is not None and noise is not None) else None

    return {
        "name":       name,
        "host":       host,
        "reachable":  True,
        "ok":         len(problems) == 0,
        "problems":   problems,
        "signal":     signal,
        "noise":      noise,
        "snr":        snr,
        "tx_rate":    tx_rate,
        "rx_rate":    rx_rate,
        "uptime":     uptime,
        "cpu":        cpu,
        "mem_pct":    round(100 * (1 - mem_free / mem_total)) if mem_total else None,
        "lan_speed":  lan_speed,
        "firmware":   firmware,
    }


@app.route("/check")
def check():
    results = [check_device(d["name"], d["host"]) for d in DEVICES]
    problems = [p for r in results for p in (r.get("problems") or ([r["error"]] if not r.get("reachable") else []))]
    return jsonify({"ok": len(problems) == 0, "devices": results, "problems": problems})


@app.route("/")
def index():
    return HTML


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
  <title>UBB Status</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%23111'/><circle cx='7' cy='21' r='3.5' fill='%234ade80'/><circle cx='25' cy='21' r='3.5' fill='%234ade80'/><path d='M10.5 21 Q16 7 21.5 21' fill='none' stroke='%234ade80' stroke-width='2.5' stroke-linecap='round'/></svg>">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #111;
      min-height: 100dvh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: flex-start;
      padding: 16px 16px 24px;
      color: #fff;
    }
    h1 { font-size: 15px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
         color: #888; margin-bottom: 16px; }

    /* Status banner */
    .banner {
      width: 100%; max-width: 420px;
      border-radius: 20px;
      padding: 18px 20px;
      text-align: center;
      margin-bottom: 20px;
      transition: background 0.3s;
    }
    .banner.loading { background: #1e1e1e; }
    .banner.ok      { background: #052e16; border: 1px solid #166534; }
    .banner.error   { background: #2d0f0f; border: 1px solid #7f1d1d; }

.banner-title { font-size: 22px; font-weight: 700; margin-bottom: 4px; }
    .banner-sub { font-size: 14px; color: #aaa; }
    .banner-problems { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; text-align: left; }
    .banner-problem { font-size: 13px; color: #fca5a5; background: rgba(0,0,0,0.2); border-radius: 8px; padding: 7px 10px; }
    .ok .banner-title  { color: #4ade80; }
    .ok .banner-sub    { color: #86efac; }
    .error .banner-title { color: #f87171; }
    .error .banner-sub   { color: #fca5a5; }

    /* Problems */
    .problems { width: 100%; max-width: 420px; margin-bottom: 16px; display: flex; flex-direction: column; gap: 8px; }
    .problem { background: #3f1515; border: 1px solid #7f1d1d; border-radius: 12px;
               padding: 12px 14px; font-size: 14px; color: #fca5a5; }

    /* Device cards */
    .devices { width: 100%; max-width: 420px; display: flex; flex-direction: column; gap: 12px; margin-bottom: 24px; }
    .device { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 16px; padding: 14px 16px; }
    .device-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
    .device-name { font-weight: 700; font-size: 15px; display: flex; align-items: baseline; gap: 7px; }
    .device-ip { font-size: 11px; color: #555; font-family: monospace; font-weight: 400; }
    .badge { font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 20px; letter-spacing: 0.04em; }
    .badge.ok    { background: #052e16; color: #4ade80; border: 1px solid #166534; }
    .badge.error { background: #3f1515; color: #f87171; border: 1px solid #7f1d1d; }
    .badge.unreachable { background: #1c1c1c; color: #888; border: 1px solid #333; }

    .stats { display: flex; flex-direction: row; gap: 6px; }
    .stat { flex: 1; background: #111; border-radius: 10px; padding: 8px 10px; min-width: 0; }
    .stat-label { font-size: 10px; color: #666; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 2px; }
    .stat-value { font-size: 13px; font-weight: 600; color: #e5e5e5; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .stat-value.good { color: #4ade80; }
    .stat-value.warn { color: #fbbf24; }
    .stat-value.bad  { color: #f87171; }
    .stat-value.dim  { color: #888; }

    .error-msg { color: #f87171; font-size: 14px; margin-top: 8px; }

    /* Button */
    .btn {
      width: 100%; max-width: 420px;
      padding: 16px;
      background: #222;
      color: #fff;
      border: 1px solid #333;
      border-radius: 14px;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s;
    }
    .btn:active { background: #2a2a2a; }
    .btn:disabled { opacity: 0.4; cursor: default; }
  </style>
</head>
<body>
  <h1>UBB Bridge Status</h1>
  <div id="banner" class="banner loading">
    <div class="spinner"></div>
    <div class="banner-title">Checking…</div>
    <div class="banner-sub">Connecting to both bridges</div>
  </div>
  <div id="problems" class="problems" style="display:none"></div>
  <div id="devices" class="devices" style="display:none"></div>
  <button class="btn" id="btn" onclick="runCheck()">Check again</button>

  <script>
    function fmt(val, unit) {
      return val != null ? `${val}${unit}` : '—';
    }

    function signalClass(s) {
      if (s == null) return 'dim';
      if (s >= -65) return 'good';
      if (s >= -80) return 'warn';
      return 'bad';
    }

    function formatUptime(s) {
      if (s == null) return '—';
      s = Math.round(s);
      const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
      if (d > 0) return `${d}d ${h}h`;
      if (h > 0) return `${h}h ${m}m`;
      return `${m}m`;
    }

    function deviceHTML(d) {
      if (!d.reachable) {
        return `<div class="device">
          <div class="device-header">
            <div class="device-name">${d.name}</div>
            <span class="badge unreachable">OFFLINE</span>
          </div>
          <div class="error-msg">${d.error}</div>
        </div>`;
      }
      const sc = signalClass(d.signal);
      const badge = d.ok ? '<span class="badge ok">OK</span>' : '<span class="badge error">ISSUE</span>';
      const tx = d.tx_rate != null ? d.tx_rate : '—';
      const rx = d.rx_rate != null ? d.rx_rate : '—';
      const speed = (d.tx_rate != null || d.rx_rate != null) ? `▲${tx} ▼${rx} Mbps` : '—';
      return `<div class="device">
        <div class="device-header">
          <div class="device-name">${d.name} <span class="device-ip">${d.host}</span></div>
          ${badge}
        </div>
        <div class="stats">
          <div class="stat" style="flex:1">
            <div class="stat-label">Signal</div>
            <div class="stat-value ${sc}">${d.signal != null ? d.signal + ' dBm' : '—'}</div>
          </div>
          <div class="stat" style="flex:2">
            <div class="stat-label">Speed</div>
            <div class="stat-value">${speed}</div>
          </div>
        </div>
        ${d.problems.length ? `<div class="banner-problems" style="margin-top:8px">${d.problems.map(p => `<div class="banner-problem">${p}</div>`).join('')}</div>` : ''}
      </div>`;
    }

    async function runCheck() {
      const banner   = document.getElementById('banner');
      const problems = document.getElementById('problems');
      const devices  = document.getElementById('devices');
      const btn      = document.getElementById('btn');

      btn.disabled = true;
      banner.className = 'banner loading';
      banner.innerHTML = '<div class="banner-title">Checking…</div>';
      problems.style.display = 'none';
      devices.style.display  = 'none';

      try {
        const res  = await fetch('/check');
        const data = await res.json();

        if (data.ok) {
          banner.className = 'banner ok';
          banner.innerHTML = '<div class="banner-title">All Good</div><div class="banner-sub">Both bridges operational</div>';
        } else {
          banner.className = 'banner error';
          banner.innerHTML = '<div class="banner-title">Problem Detected</div>';
        }

        problems.style.display = 'none';

        devices.innerHTML = data.devices.map(deviceHTML).join('');
        devices.style.display = 'flex';

      } catch (e) {
        banner.className = 'banner error';
        banner.innerHTML = '<div class="banner-icon">!</div><div class="banner-title">Error</div><div class="banner-sub">Could not reach the status service</div>';
      }

      btn.disabled = false;
    }

    runCheck();
  </script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
