#!/usr/bin/env python3
"""Collect existing QA frontend sources for offline contract discovery; GET only."""
import hashlib
import json
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qa.config import ROOT, load, request_headers, secrets
from qa.privacy import redact
from qa.transport import request


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.sources.append(attrs['src'])


def same_origin(source, candidate):
    a, b = urlsplit(source), urlsplit(candidate)
    return (b.scheme == 'https' and not b.username and not b.password
            and (a.hostname, a.port or 443) == (b.hostname, b.port or 443))


def collect(config, env, directory):
    # This is the existing supplied frontend Referer, not a guessed API route.
    headers = request_headers(env, 'A')
    page = headers.get('referer', '')
    if not same_origin('https://qa-serviceauth.mtn.co.za/', page):
        raise ValueError('Existing Referer must be on the allowed QA HTTPS origin.')
    limit = min(9, int(config['max_live_requests']))
    if limit < 1:
        raise ValueError('Configured request budget prevents collection.')
    directory.mkdir(parents=True, exist_ok=True)
    queue, seen, records = [page], set(), []
    private_values = secrets(env)
    for index in range(limit):
        if not queue:
            break
        url = queue.pop(0)
        seen.add(url)
        if index:
            time.sleep(max(0, config['min_interval_seconds']))
        response = request(url, 'GET', {**headers, 'accept': '*/*'}, None, config)
        body = response.get('raw_body', '')
        filename = f'source-{index:02d}.txt'
        (directory / filename).write_text(redact(body, private_values))
        records.append({'file': filename, 'status_code': response.get('status_code'),
                        'complete': response.get('complete', False),
                        'error': redact(response.get('error'), private_values),
                        'response_sha256': response.get('response_sha256'),
                        'url_sha256': hashlib.sha256(url.encode()).hexdigest(),
                        'conversation_mentions': body.lower().count('conversation'),
                        'uuid_mentions': body.lower().count('uuid')})
        if index == 0 and response.get('status_code') == 200 and response.get('complete'):
            parser = Scripts()
            parser.feed(body)
            for src in parser.sources:
                candidate = urljoin(page, src)
                if same_origin(page, candidate) and candidate not in seen and candidate not in queue:
                    queue.append(candidate)
        if response.get('error'):
            break
    manifest = {'purpose': 'Frontend source discovery only; no conversation created.',
                'method': 'GET', 'requests_attempted': len(records),
                'unfetched_script_count': len(queue), 'sources': records,
                'limitation': 'Redacted source text is a clue, not proof of backend conversation creation or isolation.'}
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def main():
    config, env = load()
    directory = ROOT / 'reports/conversation_discovery'
    if directory.exists():
        print('Stopped: reports/conversation_discovery already exists; preserve it for review.')
        return 2
    result = collect(config, env, directory)
    print(f"GET requests attempted: {result['requests_attempted']}. No chat POSTs sent.")
    print('Saved redacted source text and manifest: reports/conversation_discovery')
    print('Review required before implementing conversation creation or enabling client UUIDs.')
    return 0 if result['sources'] and all(s['status_code'] == 200 and s['complete'] for s in result['sources']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
