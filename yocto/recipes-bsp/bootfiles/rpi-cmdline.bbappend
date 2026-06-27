# rpi-cmdline.bb hard-assigns CMDLINE (default root=/dev/mmcblk0p2, which the
# A/B updater swaps to p3 and back). Append appliance flags: silent boot, no
# cursor, no console blanking. net.ifnames=0 keeps the wired NIC as eth0.
CMDLINE:append = " net.ifnames=0 quiet loglevel=3 vt.global_cursor_default=0 consoleblank=0"
