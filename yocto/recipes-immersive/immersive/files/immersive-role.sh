#!/bin/sh
# Boot-time role dispatch. Materialise the runtime config, then start the
# services for this node's role. render | control share one image; only the
# dispatcher is enabled, so this is the single decision point.
set -eu

/usr/bin/immersive-config.sh

ROLE=$(cat /run/immersive/role 2>/dev/null || echo render)
echo "immersive-role: starting role '$ROLE'"

case "$ROLE" in
    control)
        systemctl start immersive-clock.service
        systemctl start immersive-control.service
        ;;
    render|*)
        systemctl start immersive-render.service
        ;;
esac
