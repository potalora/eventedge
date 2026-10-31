#!/usr/bin/env python3
"""Run standalone exact-candidate real workflow acceptance; never deploy."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.live_workflow.runner import AcceptanceError, main

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except AcceptanceError as error:
        # Admission failures contain bounded reason codes, not provider exceptions.
        print('Acceptance refused: '+str(error),file=sys.stderr)
        raise SystemExit(2)
