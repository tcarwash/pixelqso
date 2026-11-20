# Pixel QSO shared desktop development

This uses the real PySide6 app, not a web recreation. Start from the dedicated UI worktree (or a clean checkout of the UI branch). The generic runtime lives in `/home/tyler/dev/desktop-dev-novnc`.

## Start

```sh
docker build --target base -t desktop-dev:base /home/tyler/dev/desktop-dev-novnc
docker compose -f compose.desktop.yaml up -d --build --wait
```

Open `http://HOST:6083/vnc.html?autoconnect=1&resize=scale` from a trusted LAN/VPN. Replace HOST with the Docker host address. The default bind is 0.0.0.0; override `DESKTOP_BIND`, `DESKTOP_PORT`, `SCREEN_WIDTH` and `SCREEN_HEIGHT` as needed. No authentication or TLS is provided: do not expose to the public internet. Everyone using this endpoint controls the same instance.

The source is mounted at /workspace; dependencies from uv.lock live outside that mount. State is stored in the dedicated pixelqso-ui named volume, not the host station profile. Initial startup disables CAT auto-connect and defaults to a disconnected loopback Data2G host and audio-only test mode. No radio, host audio, serial hardware, host home directory, or host network is passed through. Do not configure real remote Data2G or rigctld endpoints in this UI sandbox; audio test mode is not a security boundary for Data2G hosts. No proxy/auth changes are required.

## Iterate

```sh
docker compose -f compose.desktop.yaml restart desktop
docker compose -f compose.desktop.yaml logs --tail=80 desktop
docker compose -f compose.desktop.yaml exec -T desktop xwininfo -root -tree
python3 /home/tyler/dev/desktop-dev-novnc/scripts/smoke.py --port 6083
```

Rebuild with the start command after dependency or Dockerfile changes. Restart after source changes; browser users may need to reconnect. Use the real application window for visual checks, not only the HTTP healthcheck. To capture a screenshot: `docker compose -f compose.desktop.yaml exec -T desktop import -window root png:- > /tmp/pixelqso-ui.png`.

Run focused UI regression tests without interacting with the shared display:

```sh
docker compose -f compose.desktop.yaml exec -T desktop env \
  -u PIXELQSO_AUDIO_TEST -u PIXELQSO_DATA2G_HOST -u PIXELQSO_DATA2G_LOCAL_HOST \
  -u PIXELQSO_DATA2G_RIGCTLD_HOST -u PIXELQSO_RIGCTLD_HOST -u PIXELQSO_TEST_LINK \
  QT_QPA_PLATFORM=offscreen /opt/app-venv/bin/python -m pytest -q \
  tests/test_desktop_workspace.py tests/test_canvas_tools.py tests/test_animation_editor.py
```

The fixtures create temporary isolated profiles. Keep selection and navigation separate from explicit transmit actions. Preserve radio-control/backend safety gates and do not run real on-air tests from the sandbox.

Stop using `docker compose -f compose.desktop.yaml down`; do not add -v unless intentionally discarding development cards and settings.
