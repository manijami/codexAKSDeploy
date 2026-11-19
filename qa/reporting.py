"""Portable, escaped reports; only redacted captures are persisted."""
import csv
import html
import json
from collections import Counter
from pathlib import Path
from .privacy import redact


def write_report(directory, results, metadata, secrets=()):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    clean = redact({'metadata': metadata, 'results': results}, secrets)
    summary = dict(Counter(r['status'] for r in results))
    clean['summary'] = summary
    (directory / 'results.json').write_text(json.dumps(clean, indent=2, ensure_ascii=False) + '\n')
    with (directory / 'results.csv').open('w', newline='') as file:
        fields = ['test_id', 'category', 'status', 'request_payload', 'actual_api_response', 'metric_scores', 'defect_description', 'observations']
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for result in clean['results']:
            writer.writerow({k: json.dumps(result.get(k), ensure_ascii=False) if isinstance(result.get(k), (list, dict)) else result.get(k, '') for k in fields})
    lines = ['# Morpheus WhatsApp QA results', '', 'Mode: ' + metadata['mode'], '',
             'Synthetic/demo results do not assess the MTN service.' if metadata['mode'] == 'demo' else 'Captured responses are redacted. Null scores mean unassessed.', '',
             '| Test ID | Category | Status | Defect or missing evidence |', '|---|---|---|---|']
    for r in clean['results']:
        esc = lambda s: str(s).replace('|', '\\|').replace('\n', ' ')
        lines.append('| ' + ' | '.join(esc(r.get(k, '')) for k in ('test_id', 'category', 'status', 'defect_description')) + ' |')
    lines += ['', 'Full payloads, responses, metric methods and observations are in results.json and report.html.', '', 'Counts: ' + json.dumps(summary)]
    (directory / 'report.md').write_text('\n'.join(lines) + '\n')
    cards = []
    for r in clean['results']:
        cards.append('<details><summary>' + html.escape(f"{r['test_id']} · {r['category']} · {r['status']}") + '</summary><pre>' + html.escape(json.dumps(r, indent=2, ensure_ascii=False)) + '</pre></details>')
    (directory / 'report.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Morpheus QA report</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;color:#171717}h1{border-left:10px solid #ffcb05;padding-left:16px}details{padding:14px;border:1px solid #ddd;margin:10px 0;border-radius:8px}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}small{color:#666}</style><h1>Morpheus WhatsApp QA report</h1><p>Mode: ' + html.escape(metadata['mode']) + '</p><p>' + html.escape(json.dumps(summary)) + '</p><small>Null metric scores are unassessed. Demo data is synthetic. These reports do not certify compliance.</small>' + ''.join(cards) + '</html>')
    for file in directory.iterdir():
        if file.is_file():
            file.chmod(0o600)
    return directory
