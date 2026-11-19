#!/usr/bin/env python3
"""Run ten read-only FAQ questions using the observed frontend UUID workflow."""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qa.config import ROOT
from qa.faq_pilot import catalogue, digest, validate_dataset
from qa.runner import run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--out', type=Path, default=ROOT / 'reports/faq_pilot')
    args = parser.parse_args(argv)
    if args.out.exists():
        raise ValueError('Output directory exists; choose a new directory to preserve captures.')
    path = ROOT / 'datasets/faq_pilot.json'
    data = validate_dataset(json.loads(path.read_text()), check_documents=True)
    args.out.mkdir(parents=True)
    snapshot = args.out / 'dataset.snapshot.json'
    snapshot.write_bytes(path.read_bytes())
    options = SimpleNamespace(suite='all', id=None, limit=None, live=args.live,
                              demo=False, allow_writes=False, evidence=None,
                              out=str(args.out), frontend_fresh=True)
    print('References require business review. Retrieval evidence is not supplied by the reference passages.')
    return run(options, cases=catalogue(data), metadata={'pilot_dataset_sha256': digest(snapshot),
               'pilot_reference_status': data['reference_status']})


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError):
        print('Pilot stopped: check dataset/source integrity, output directory and local configuration. No automatic retry.', file=sys.stderr)
        raise SystemExit(2)
