# AGENTS.md

Context for agents working on this repo.

## What this is

A small Flask web app that reports the status of a pair of Ubiquiti UBB
(UniFi Building Bridge) point-to-point wireless bridges by SSHing into them
and parsing `mca-status` output. It also exposes a "restart" flow that power
cycles the near-end bridge via a Tapo P110 smart plug, and lets you set the
LED colour on each bridge from the browser.

The whole app is one file: `app.py`. The HTML/CSS/JS lives in a triple-quoted
string at the bottom.

## Devices

- **Near-end UBB**: `192.168.4.37`
- **Far-end UBB**:  `192.168.4.38`
- **Tapo P110 plug** (powers near-end): `192.168.4.35`
- SSH user on both UBBs: `jumble.jumble`
- SSH key path inside the container: `/run/secrets/ubb_key` (mounted as a
  Docker secret from `/opt/stacks/media/ubb-status/ubb_key` on the host)

## Endpoints

- `GET /`              — single-page UI
- `GET /check`         — JSON: queries both bridges via SSH, returns status,
                         problems, signal, speeds, LED colour, etc.
- `POST /led`          — `{host, color}`, SSHes to write
                         `/proc/ubnt_ledbar/color`
- `POST /tapo/off`     — power off the Tapo plug (kills near-end UBB)
- `POST /tapo/on`      — power on the Tapo plug
- `GET /static/favicon.png`

## How a check works

For each device, we open one SSH session that runs:

```
mca-status; echo '__LED__'; cat /proc/ubnt_ledbar/color
```

The output is split on `__LED__`. `mca-status` returns key=value lines
(sometimes comma-separated on the same line). The LED file returns
`r,g,b (#rrggbb)`.

## Restart flow (frontend-driven)

The button only appears on the near-end card when there are problems. Steps:

1. Confirm modal
2. `POST /tapo/off`             — stage: "Powering down"
3. wait 5 s, `POST /tapo/on`    — stage: "Restarting"
4. poll `GET /check` every 3 s until near-end is reachable — stage: "Waiting"
5. stage: "Success", refresh main UI

## UI layout

Bridge view is a 3-column grid: near | link | far.

- Top row: white disc with a status icon inside (tick / cross / dash).
  - The disc's inner border (1.5 px, rgba(0,0,0,0.5)) is fixed.
  - An outer 4 px box-shadow ring is the bridge's current LED colour.
  - Tapping the disc opens a native colour picker; selection POSTs to `/led`.
- Centre column: `→ Mbps`, `← Mbps`, SNR.
- Below each disc: host IP, signal dBm, CPU%, problem badges, restart button
  (near-end, only when problems).

## Environment variables

Read from `/opt/stacks/media/docker-compose.yml`:

- `TAPO_USER`, `TAPO_PASS`, `TAPO_HOST`
- `SSH_KEY_PATH` (defaults to `/run/secrets/ubb_key`)
- `PORT` (defaults to 8080)

## Deployment

CI publishes a Docker image to GitHub Container Registry on every push to
`main`:

- Workflow: `.github/workflows/docker.yml`
- Image:    `ghcr.io/jumblejumble/ubb-status:latest`

The image runs on the media server at `192.168.7.173`, defined in
`/opt/stacks/media/docker-compose.yml` (service name `ubb-status`, port
9569 → 8080). SSH into it as `richard@192.168.7.173`. The compose file is
root-owned; use `sudo` to edit or run compose commands.

Typical deploy after a code push:

```bash
ssh richard@192.168.7.173
sudo docker compose -f /opt/stacks/media/docker-compose.yml pull ubb-status
sudo docker compose -f /opt/stacks/media/docker-compose.yml up -d --force-recreate ubb-status
```

The `pull` is required even though `:latest` is reused — Docker caches the
tag locally and `up -d` alone will not fetch a new digest.

## Git remote

The `origin` remote uses an aliased Host (`github-jumblejumble`) defined in
`~/.ssh/config` so the right SSH key is used. Don't change the remote URL
to plain `git@github.com:...` — that picks up the user's other GitHub
identity and the push will be rejected.

## Files

- `app.py`                       — the entire app (backend + HTML/JS)
- `Dockerfile`                   — python:3.12-slim + openssh-client
- `requirements.txt`             — `flask`, `tapo`
- `static/favicon.png`           — favicon (real PNG file, served at
                                   `/static/favicon.png` — mobile browsers
                                   cache the inline-SVG variant badly)
- `.github/workflows/docker.yml` — builds + pushes to ghcr.io
- `main.py`                      — UNRELATED. Old LED gradient script that
                                   opens a persistent SSH shell to each
                                   bridge and writes RGB values in a loop.
                                   Not used by the web app.

## Gotchas

- The UBB returns `r,g,b (#rrggbb)` from `/proc/ubnt_ledbar/color` — parse
  the hex out, don't try to `int()` the trailing `(#...)`.
- The Tapo `python-tapo` client is async (`asyncio.run` from sync Flask
  handlers is fine here — Flask requests are one-shot).
- The compose file on the media server is **root-owned**. `sudo` is needed
  to edit it; the user's password is required for sudo (no NOPASSWD).
- Don't commit Tapo credentials. They live in env vars in the compose file
  on the server, not in this repo.
