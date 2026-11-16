"""Retired legacy scanner. This entry point must not capture or apply data.

The former implementation is preserved in repository history and the local
retirement audit. Seamcal is a separate, experimental research tool; neither
workflow constitutes a validated automatic calibration of the Dortmund room.
"""
import sys

RETIRED = "Legacy autocalib is retired and must not be used. No camera, projector or room configuration was changed."

async def run(args):
    raise RuntimeError(RETIRED)

async def push_and_save(link, entries):
    raise RuntimeError(RETIRED)

def main(argv=None):
    print(RETIRED, file=sys.stderr)
    return 2

if __name__ == '__main__':
    raise SystemExit(main())
