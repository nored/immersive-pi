#!/bin/sh
# Calibration page: http://localhost:8099 on this computer, https://<this computer>.local:8443 for a phone.
# CTL_HOST = controller, MESH / CORNER_MESH = warp mesh size per projector.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
CTL_HOST="${CTL_HOST:-dortmund-control.local}" MESH="${MESH:-8x8}" CORNER_MESH="${CORNER_MESH:-8x8}" exec .venv/bin/python calib_server.py
