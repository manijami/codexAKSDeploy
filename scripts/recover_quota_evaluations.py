#!/usr/bin/env python3
"""Prepare or execute quota-only saved-answer evaluation recovery."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa.evaluator_recovery import main
if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError,RuntimeError):
        print('Recovery stopped: configuration, deadline or integrity checks failed. No credential values displayed; original campaign unchanged.',file=sys.stderr)
        raise SystemExit(2)
