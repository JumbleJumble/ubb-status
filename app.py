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
            ["ssh", *SSH_OPTS, f"{SSH_USER}@{host}", "mca-status; echo '__IW__'; iw dev wlan0 station dump 2>/dev/null; echo '__LED__'; cat /proc/ubnt_ledbar/color 2>/dev/null || true"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {"name": name, "host": host, "reachable": False, "error": "Connection timed out"}

    if r.returncode != 0:
        return {"name": name, "host": host, "reachable": False, "error": "SSH failed"}

    # Split sections
    parts = r.stdout.split("__IW__")
    mca_out = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    iw_led = rest.split("__LED__")
    iw_out  = iw_led[0]
    led_raw = iw_led[1].strip() if len(iw_led) > 1 else ""

    led_color = None
    if led_raw:
        try:
            # Format is "r,g,b (#rrggbb)" — grab the hex directly if present
            import re as _re
            m = _re.search(r'\(#([0-9a-fA-F]{6})\)', led_raw)
            if m:
                led_color = "#" + m.group(1)
            else:
                rgb_part = led_raw.split("(")[0].strip()
                rv, gv, bv = [int(x.strip()) for x in rgb_part.split(",")]
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

    # Parse 60GHz rates from `iw dev wlan0 station dump` — these are the real link rates.
    # mca-status wlanTxRate/wlanRxRate only reflect the 5GHz fallback radio.
    import re as _re
    _iw_tx = _re.search(r'tx bitrate:\s+([\d.]+)\s+MBit/s', iw_out)
    _iw_rx = _re.search(r'rx bitrate:\s+([\d.]+)\s+MBit/s', iw_out)

    signal      = num("signal")
    noise       = num("noise")
    tx_rate     = round(float(_iw_tx.group(1))) if _iw_tx else num("wlanTxRate")
    rx_rate     = round(float(_iw_rx.group(1))) if _iw_rx else num("wlanRxRate")
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
      background: #111; color: #fff;
      min-height: 100dvh;
      display: flex; flex-direction: column; align-items: center;
      justify-content: flex-start;
      padding: 16px 16px 24px;
    }
    h1 { font-size: 13px; font-weight: 600; letter-spacing: 0.08em;
         text-transform: uppercase; color: #555; margin-bottom: 12px; }

    /* Status chip */
    .chip {
      font-size: 12px; font-weight: 700; padding: 4px 14px;
      border-radius: 20px; margin-bottom: 20px; letter-spacing: 0.05em;
    }
    .chip.loading { background: #1e1e1e; color: #555; }
    .chip.ok      { background: #052e16; color: #4ade80; border: 1px solid #166534; }
    .chip.error   { background: #2d0f0f; color: #f87171; border: 1px solid #7f1d1d; }

    /* Bridge grid: near | link | far */
    .bridge {
      width: 100%; max-width: 440px;
      display: grid;
      grid-template-columns: 1fr 80px 1fr;
      column-gap: 8px;
      row-gap: 16px;
      margin-bottom: 20px;
    }

    /* Node top: circle + label */
    .node-head { display: flex; flex-direction: column; align-items: center; gap: 7px; }
    .node-circle {
      width: 66px; height: 66px; border-radius: 50%;
      background: #ffffff;
      border: 1.5px solid rgba(0,0,0,0.5);
      display: flex; align-items: center; justify-content: center;
      transition: box-shadow 0.3s;
      flex-shrink: 0;
      position: relative; overflow: hidden;
    }
    label.node-circle { cursor: pointer; }
    .node-circle input[type=color] {
      position: absolute; width: 200%; height: 200%; opacity: 0; cursor: pointer;
    }
    .node-label { font-size: 11px; font-weight: 700; color: #555;
                  text-transform: uppercase; letter-spacing: 0.07em; }

    /* Link centre: speeds + SNR */
    .link-head {
      display: flex; flex-direction: column;
      align-items: center; justify-content: center; gap: 6px;
    }
    .speed-row { display: flex; align-items: baseline; gap: 3px; line-height: 1; }
    .speed-dir  { font-size: 11px; color: #444; }
    .speed-num  { font-size: 15px; font-weight: 700; color: #e5e5e5; }
    .speed-unit { font-size: 10px; color: #555; }
    .link-snr   { font-size: 11px; color: #555; margin-top: 2px; }

    /* Node detail rows */
    .node-detail {
      display: flex; flex-direction: column; align-items: center; gap: 6px;
    }
    .drow { font-size: 12px; color: #666; display: flex; align-items: center; gap: 5px; }
    .dval { font-weight: 600; color: #ccc; }
    .dval.good { color: #4ade80; }
    .dval.warn { color: #fbbf24; }
    .dval.bad  { color: #f87171; }
    .dval.mono { font-family: monospace; font-size: 11px; letter-spacing: -0.02em; }

    .d-problems { display: flex; flex-direction: column; gap: 3px; width: 100%; }
    .d-problem  { font-size: 11px; color: #fca5a5;
                  background: rgba(127,29,29,0.3); border-radius: 6px;
                  padding: 4px 8px; text-align: center; }

    /* Restart button */
    .btn-restart {
      width: 100%; padding: 7px 8px;
      background: #1a1a1a; color: #f87171;
      border: 1px solid #7f1d1d; border-radius: 8px;
      font-size: 12px; font-weight: 600; cursor: pointer;
      transition: background 0.15s;
    }
    .btn-restart:active { background: #2d0f0f; }

    /* Check button */
    .btn {
      width: 100%; max-width: 440px; padding: 16px;
      background: #222; color: #fff;
      border: 1px solid #333; border-radius: 14px;
      font-size: 16px; font-weight: 600; cursor: pointer;
      transition: background 0.15s;
    }
    .btn:active   { background: #2a2a2a; }
    .btn:disabled { opacity: 0.4; cursor: default; }

    /* Confirm modal */
    .modal-backdrop {
      position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 50;
      display: flex; align-items: center; justify-content: center; padding: 24px;
    }
    .modal {
      background: #1a1a1a; border: 1px solid #333; border-radius: 20px;
      padding: 24px; width: 100%; max-width: 360px;
    }
    .modal-title { font-size: 17px; font-weight: 700; margin-bottom: 8px; }
    .modal-body  { font-size: 14px; color: #aaa; margin-bottom: 20px; line-height: 1.5; }
    .modal-actions { display: flex; gap: 10px; }
    .modal-actions button { flex: 1; padding: 12px; border-radius: 12px;
                            font-size: 15px; font-weight: 600; cursor: pointer; border: none; }
    .btn-cancel  { background: #222; color: #aaa; border: 1px solid #333 !important; }
    .btn-confirm { background: #7f1d1d; color: #fca5a5; }

    /* Restart overlay */
    @keyframes spin { to { transform: rotate(360deg); } }
    .spinner {
      width: 48px; height: 48px; border-radius: 50%;
      border: 3px solid #2a2a2a; border-top-color: #4ade80;
      animation: spin 0.9s linear infinite;
    }
    #restart-overlay {
      position: fixed; inset: 0; background: #111; z-index: 100;
      display: none; flex-direction: column;
      align-items: center; justify-content: center; gap: 20px;
    }
    .restart-title { font-size: 24px; font-weight: 700; }
    .stage-list  { display: flex; flex-direction: column; gap: 8px; }
    .stage-item  { font-size: 13px; color: #444; display: flex; align-items: center; gap: 8px; }
    .stage-item.done   { color: #4ade80; }
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
  <div id="chip" class="chip loading">Checking…</div>
  <div id="bridge" class="bridge" style="display:none"></div>
  <button class="btn" id="btn" onclick="runCheck()">Check again</button>

  <script>
    const TICK    = `<svg width="28" height="28" viewBox="0 0 24 24" fill="none"><path d="M5 12l5 5L19 7" stroke="#16a34a" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    const CROSS   = `<svg width="28" height="28" viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6L6 18" stroke="#dc2626" stroke-width="2.5" stroke-linecap="round"/></svg>`;
    const OFFLINE = `<svg width="28" height="28" viewBox="0 0 24 24" fill="none"><path d="M8 12h8" stroke="#aaa" stroke-width="2.5" stroke-linecap="round"/></svg>`;

    function sc(s) {
      if (s == null) return '';
      return s >= -65 ? 'good' : s >= -80 ? 'warn' : 'bad';
    }

    function nodeHeadHTML(d) {
      const label = d ? d.name.replace('-end', '') : '?';
      if (!d || !d.reachable) {
        return `<div class="node-head">
          <div class="node-circle" style="box-shadow:0 0 0 4px #666">${OFFLINE}</div>
          <div class="node-label">${label}</div>
        </div>`;
      }
      const icon  = d.ok ? TICK : CROSS;
      const ring  = d.led_color || (d.ok ? '#4ade80' : '#f87171');
      const shadow = `box-shadow:0 0 0 4px ${ring}`;
      const circle = d.led_color
        ? `<label class="node-circle" style="${shadow}">${icon}<input type="color" value="${d.led_color}" onchange="setLed('${d.host}',this)"></label>`
        : `<div class="node-circle" style="${shadow}">${icon}</div>`;
      return `<div class="node-head">${circle}<div class="node-label">${label}</div></div>`;
    }

    function linkHTML(near, far) {
      const toFar  = near?.tx_rate  ?? far?.rx_rate  ?? null;
      const toNear = near?.rx_rate  ?? far?.tx_rate  ?? null;
      const snr    = near?.snr      ?? far?.snr      ?? null;
      const f = v => v != null ? v : '—';
      return `<div class="link-head">
        <div class="speed-row">
          <span class="speed-dir">→</span>
          <span class="speed-num">${f(toFar)}</span>
          <span class="speed-unit">Mbps</span>
        </div>
        <div class="speed-row">
          <span class="speed-dir">←</span>
          <span class="speed-num">${f(toNear)}</span>
          <span class="speed-unit">Mbps</span>
        </div>
        ${snr != null ? `<div class="link-snr">SNR ${snr.toFixed(0)} dB</div>` : ''}
      </div>`;
    }

    function nodeDetailHTML(d, isNear) {
      if (!d || !d.reachable) {
        return `<div class="node-detail">
          <div class="drow" style="color:#f87171">${d ? d.error : 'No data'}</div>
        </div>`;
      }
      const problems = d.problems?.length
        ? `<div class="d-problems">${d.problems.map(p=>`<div class="d-problem">${p}</div>`).join('')}</div>`
        : '';
      const restart = isNear && d.problems?.length
        ? `<button class="btn-restart" onclick="confirmRestart()">Restart</button>`
        : '';
      return `<div class="node-detail">
        <div class="drow"><span class="dval mono">${d.host}</span></div>
        ${d.signal != null ? `<div class="drow"><span class="dval ${sc(d.signal)}">${d.signal} dBm</span></div>` : ''}
        ${d.cpu    != null ? `<div class="drow">CPU <span class="dval">${d.cpu.toFixed(0)}%</span></div>` : ''}
        ${problems}
        ${restart}
      </div>`;
    }

    function renderBridge(data) {
      const near = data.devices.find(d => d.name === 'Near-end');
      const far  = data.devices.find(d => d.name === 'Far-end');
      return nodeHeadHTML(near) + linkHTML(near, far) + nodeHeadHTML(far)
           + nodeDetailHTML(near, true) + '<div></div>' + nodeDetailHTML(far, false);
    }

    async function setLed(host, input) {
      input.closest('.node-circle').style.boxShadow = `0 0 0 4px ${input.value}`;
      await fetch('/led', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({host, color: input.value}),
      });
    }

    function confirmRestart() { document.getElementById('confirm-modal').style.display = 'flex'; }
    function closeConfirm()   { document.getElementById('confirm-modal').style.display = 'none'; }

    function setStage(id) {
      ['stage-powerdown','stage-restarting','stage-waiting','stage-success'].forEach((s,i,arr) => {
        const idx = arr.indexOf(id);
        document.getElementById(s).className = 'stage-item' + (i < idx ? ' done' : i === idx ? ' active' : '');
      });
    }

    const sleep = ms => new Promise(r => setTimeout(r, ms));

    async function waitForNearEnd() {
      for (let i = 0; i < 40; i++) {
        await sleep(3000);
        try {
          const d = await fetch('/check').then(r => r.json());
          if (d.devices.find(x => x.name === 'Near-end')?.reachable) return;
        } catch (_) {}
      }
      throw new Error('Timed out');
    }

    async function doRestart() {
      closeConfirm();
      document.getElementById('restart-overlay').style.display = 'flex';
      try {
        setStage('stage-powerdown');  await fetch('/tapo/off', {method:'POST'});
        setStage('stage-restarting'); await sleep(5000); await fetch('/tapo/on', {method:'POST'});
        setStage('stage-waiting');    await waitForNearEnd();
        setStage('stage-success');    await sleep(1500);
      } catch(e) {
        document.getElementById('stage-waiting').textContent = 'Error: ' + e.message;
        await sleep(3000);
      }
      document.getElementById('restart-overlay').style.display = 'none';
      runCheck();
    }

    async function runCheck() {
      const chip = document.getElementById('chip');
      const bridge = document.getElementById('bridge');
      const btn = document.getElementById('btn');
      btn.disabled = true;
      chip.className = 'chip loading'; chip.textContent = 'Checking…';
      bridge.style.display = 'none';
      try {
        const data = await fetch('/check').then(r => r.json());
        chip.className = 'chip ' + (data.ok ? 'ok' : 'error');
        chip.textContent = data.ok ? 'All Good'
          : data.problems.length + ' issue' + (data.problems.length !== 1 ? 's' : '');
        bridge.innerHTML = renderBridge(data);
        bridge.style.display = 'grid';
      } catch(e) {
        chip.className = 'chip error'; chip.textContent = 'Could not reach status service';
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
