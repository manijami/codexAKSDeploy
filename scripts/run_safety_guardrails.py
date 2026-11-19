#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa.safety_pack import main
if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError,RuntimeError):
        print('Safety campaign refused: integrity, private configuration, package or prerequisite check failed. No private values displayed.',file=sys.stderr);raise SystemExit(2)
