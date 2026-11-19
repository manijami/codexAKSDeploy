#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa.safety_response_continuation import main
if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError,RuntimeError):
        print('Safety continuation refused: parent evidence, source, private configuration, packages or limits changed. No private values displayed.',file=sys.stderr);raise SystemExit(2)
