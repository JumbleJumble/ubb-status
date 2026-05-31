#!/usr/bin/env python3
import asyncio
import os
import subprocess
from flask import Flask, jsonify, request
from tapo import ApiClient

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

TAPO_USER = os.environ.get("TAPO_USER", "")
TAPO_PASS = os.environ.get("TAPO_PASS", "")
TAPO_HOST = os.environ.get("TAPO_HOST", "192.168.4.35")

SIGNAL_WARN  = -65
SIGNAL_ERROR = -80


def check_device(name, host):
    try:
        r = subprocess.run(
            ["ssh", *SSH_OPTS, f"{SSH_USER}@{host}", "mca-status; echo '__LED__'; cat /proc/ubnt_ledbar/color 2>/dev/null || true"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {"name": name, "host": host, "reachable": False, "error": "Connection timed out"}

    if r.returncode != 0:
        return {"name": name, "host": host, "reachable": False, "error": "SSH failed"}

    # Split mca-status output from LED color
    parts = r.stdout.split("__LED__")
    mca_out = parts[0]
    led_raw = parts[1].strip() if len(parts) > 1 else ""

    led_color = None
    if led_raw:
        try:
            rv, gv, bv = [int(x) for x in led_raw.split(",")]
            led_color = f"#{rv:02x}{gv:02x}{bv:02x}"
        except (ValueError, TypeError):
            pass

    # Parse key=value (first line may have comma-separated device info)
    data = {}
    for line in mca_out.strip().splitlines():
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
        "led_color":  led_color,
    }


@app.route("/led", methods=["POST"])
def set_led():
    body = request.json or {}
    host = body.get("host", "")
    color = body.get("color", "").lstrip("#")

    valid_hosts = {d["host"] for d in DEVICES}
    if host not in valid_hosts or len(color) != 6:
        return jsonify({"ok": False, "error": "invalid params"}), 400

    try:
        rv, gv, bv = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid color"}), 400

    r = subprocess.run(
        ["ssh", *SSH_OPTS, f"{SSH_USER}@{host}", f"echo '{rv},{gv},{bv}' > /proc/ubnt_ledbar/color"],
        capture_output=True, text=True, timeout=10,
    )
    return jsonify({"ok": r.returncode == 0})


async def _tapo_set(on: bool):
    client = ApiClient(TAPO_USER, TAPO_PASS)
    device = await client.p110(TAPO_HOST)
    if on:
        await device.on()
    else:
        await device.off()


@app.route("/tapo/off", methods=["POST"])
def tapo_off():
    asyncio.run(_tapo_set(False))
    return jsonify({"ok": True})


@app.route("/tapo/on", methods=["POST"])
def tapo_on():
    asyncio.run(_tapo_set(True))
    return jsonify({"ok": True})


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
  <link rel="icon" type="image/png" href="/static/favicon.png">
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

    /* LED swatch */
    .led-swatch {
      position: relative; width: 22px; height: 22px;
      border-radius: 50%; cursor: pointer; flex-shrink: 0;
      border: 2px solid rgba(255,255,255,0.15);
      display: inline-flex; align-items: center; justify-content: center;
      overflow: hidden;
    }
    .led-swatch input[type=color] {
      position: absolute; width: 200%; height: 200%; opacity: 0; cursor: pointer;
    }

    /* Restart button */
    .btn-restart {
      margin-top: 10px; width: 100%;
      padding: 9px 14px;
      background: #1a1a1a; color: #f87171;
      border: 1px solid #7f1d1d; border-radius: 10px;
      font-size: 13px; font-weight: 600; cursor: pointer;
      transition: background 0.15s;
    }
    .btn-restart:active { background: #2d0f0f; }

    /* Confirm modal */
    .modal-backdrop {
      position: fixed; inset: 0; background: rgba(0,0,0,0.7);
      z-index: 50; display: flex; align-items: center; justify-content: center;
      padding: 24px;
    }
    .modal {
      background: #1a1a1a; border: 1px solid #333; border-radius: 20px;
      padding: 24px; width: 100%; max-width: 360px;
    }
    .modal-title { font-size: 17px; font-weight: 700; margin-bottom: 8px; }
    .modal-body  { font-size: 14px; color: #aaa; margin-bottom: 20px; line-height: 1.5; }
    .modal-actions { display: flex; gap: 10px; }
    .modal-actions button { flex: 1; padding: 12px; border-radius: 12px; font-size: 15px; font-weight: 600; cursor: pointer; border: none; }
    .btn-cancel  { background: #222; color: #aaa; border: 1px solid #333 !important; }
    .btn-confirm { background: #7f1d1d; color: #fca5a5; }

    /* Restart overlay */
    @keyframes spin { to { transform: rotate(360deg); } }
    .spinner {
      width: 48px; height: 48px;
      border: 3px solid #2a2a2a; border-top-color: #4ade80;
      border-radius: 50%; animation: spin 0.9s linear infinite;
    }
    #restart-overlay {
      position: fixed; inset: 0; background: #111; z-index: 100;
      display: none; flex-direction: column;
      align-items: center; justify-content: center; gap: 20px;
    }
    .restart-title { font-size: 24px; font-weight: 700; color: #fff; }
    .restart-stage { font-size: 14px; color: #666; }
    .restart-stage.active { color: #4ade80; }
    .stage-list { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
    .stage-item {
      font-size: 13px; color: #444; display: flex; align-items: center; gap: 8px;
    }
    .stage-item.done  { color: #4ade80; }
    .stage-item.active { color: #fff; }
    .stage-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
  </style>
</head>
<body>
  <!-- Restart overlay -->
  <div id="restart-overlay">
    <div class="spinner"></div>
    <div class="restart-title">Restarting</div>
    <div class="stage-list">
      <div class="stage-item" id="stage-powerdown"><div class="stage-dot"></div>Powering down</div>
      <div class="stage-item" id="stage-restarting"><div class="stage-dot"></div>Restarting</div>
      <div class="stage-item" id="stage-waiting"><div class="stage-dot"></div>Waiting for bridge</div>
      <div class="stage-item" id="stage-success"><div class="stage-dot"></div>Success</div>
    </div>
  </div>

  <!-- Confirm modal -->
  <div id="confirm-modal" class="modal-backdrop" style="display:none">
    <div class="modal">
      <div class="modal-title">Restart Near-end?</div>
      <div class="modal-body">This will power cycle the UBB via the loft plug. The bridge will be offline for ~30 seconds.</div>
      <div class="modal-actions">
        <button class="btn-cancel" onclick="closeConfirm()">Cancel</button>
        <button class="btn-confirm" onclick="doRestart()">Restart</button>
      </div>
    </div>
  </div>

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
      const swatch = d.led_color
        ? `<label class="led-swatch" style="background:${d.led_color}" title="Set LED colour">
             <input type="color" value="${d.led_color}" onchange="setLed('${d.host}', this)">
           </label>`
        : '';
      return `<div class="device">
        <div class="device-header">
          <div class="device-name">${d.name} <span class="device-ip">${d.host}</span></div>
          <div style="display:flex;align-items:center;gap:8px">${swatch}${badge}</div>
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
      ${d.name === 'Near-end' ? '<button class="btn-restart" onclick="confirmRestart()">Restart</button>' : ''}
      </div>`;
    }

    async function setLed(host, input) {
      input.closest('.led-swatch').style.background = input.value;
      await fetch('/led', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({host, color: input.value}),
      });
    }

    function confirmRestart() {
      document.getElementById('confirm-modal').style.display = 'flex';
    }

    function closeConfirm() {
      document.getElementById('confirm-modal').style.display = 'none';
    }

    function setStage(id) {
      const ids = ['stage-powerdown', 'stage-restarting', 'stage-waiting', 'stage-success'];
      const idx = ids.indexOf(id);
      ids.forEach((s, i) => {
        const el = document.getElementById(s);
        el.className = 'stage-item' + (i < idx ? ' done' : i === idx ? ' active' : '');
      });
    }

    function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

    async function waitForNearEnd() {
      for (let i = 0; i < 40; i++) {
        await sleep(3000);
        try {
          const res = await fetch('/check');
          const data = await res.json();
          const near = data.devices.find(d => d.name === 'Near-end');
          if (near && near.reachable) return;
        } catch (_) {}
      }
      throw new Error('Timed out');
    }

    async function doRestart() {
      closeConfirm();

      const overlay = document.getElementById('restart-overlay');
      overlay.style.display = 'flex';

      try {
        setStage('stage-powerdown');
        await fetch('/tapo/off', { method: 'POST' });

        setStage('stage-restarting');
        await sleep(5000);
        await fetch('/tapo/on', { method: 'POST' });

        setStage('stage-waiting');
        await waitForNearEnd();

        setStage('stage-success');
        await sleep(1500);
      } catch (e) {
        document.getElementById('stage-waiting').textContent = 'Error: ' + e.message;
        await sleep(3000);
      }

      overlay.style.display = 'none';
      runCheck();
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
