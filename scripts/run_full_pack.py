#!/usr/bin/env python3
"""Entry point for the bounded full QA campaign."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa.campaign import main
if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError):
        print('Campaign stopped: invalid/mismatched input or configuration; no secret details printed.',file=sys.stderr)
        raise SystemExit(2)
