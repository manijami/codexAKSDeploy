#!/usr/bin/env python3
"""Prepare/execute only the exact pending IDs in the linked campaign manifest."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa.linked_continuation import main
if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError,RuntimeError):
        print('Continuation refused: source, selection, credentials, package, limit or deadline integrity check failed. No private values displayed.',file=sys.stderr)
        raise SystemExit(2)
