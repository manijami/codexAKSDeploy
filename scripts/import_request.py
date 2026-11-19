#!/usr/bin/env python3
"""Import a local Postman/curl command; persist secrets in gitignored .env only."""
import argparse
import json
import shlex
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qa.config import ROOT


def parse_command(command):
    tokens = shlex.split(command.replace('\\\n', ' '))
    headers, body, url = {}, None, None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ('--header', '-H', '--body', '--data', '--data-raw', '--data-binary', '-d', '--url'):
            if i + 1 == len(tokens):
                raise ValueError('Missing command argument.')
            value = tokens[i + 1]
            if token in ('--header', '-H'):
                name, sep, text = value.partition(':')
                if not sep:
                    raise ValueError('Malformed header.')
                headers[name.strip().lower()] = text.strip()
            elif token == '--url':
                url = value
            else:
                if value.startswith('@'):
                    raise ValueError('Use an inline JSON body, not @file.')
                body = json.loads(value)
            i += 2
            continue
        if token.startswith('https://'):
            url = token
        i += 1
    if not url or not isinstance(body, dict):
        raise ValueError('Need an HTTPS URL and inline JSON --body/--data-raw.')
    parsed = urlsplit(url)
    if parsed.hostname != 'qa-serviceauth.mtn.co.za' or parsed.path != '/morpheus-v5/api/prepaid/text':
        raise ValueError('Command must target the supplied Morpheus QA endpoint.')
    for field in ('message', 'msisdn', 'conversationId'):
        if not isinstance(body.get(field), str) or not body[field]:
            raise ValueError('Missing required string field: ' + field)
    cookie_parts = {}
    for part in headers.pop('cookie', '').split(';'):
        name, sep, value = part.strip().partition('=')
        if sep:
            cookie_parts[name] = value  # Same-name cookies: preserve the last captured value.
    headers = {k:v for k,v in headers.items() if k not in ('host','content-length','connection','accept-encoding')}
    env = {'MORPHEUS_BASE_URL': 'https://qa-serviceauth.mtn.co.za',
           'MORPHEUS_API_KEY': parse_qs(parsed.query).get('key', [''])[0],
           'MORPHEUS_COOKIE': '; '.join(k + '=' + v for k,v in cookie_parts.items()),
           'MORPHEUS_HEADERS_JSON': json.dumps(headers),
           'QA_MSISDN_A': body['msisdn'], 'QA_CONVERSATION_ID_A': body['conversationId'],
           'QA_SUBSCRIBER_TYPE_A': body.get('subscriberType','POSTPAID')}
    return env


def save_env(updates, root=ROOT):
    values = {}
    for path in (root / '.env.example', root / '.env'):
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip() and not line.lstrip().startswith('#'):
                    k, sep, v = line.partition('=')
                    if sep:
                        values[k] = v
    values.update(updates)
    if any('\n' in str(v) or '\r' in str(v) for v in values.values()):
        raise ValueError('Environment values must be single-line literals.')
    path = root / '.env'
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text('# Local private QA settings; never commit this file.\n' + ''.join(k+'='+str(v)+'\n' for k,v in values.items()))
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path, help='Local text file containing the copied request command.')
    args = parser.parse_args()
    try:
        updates = parse_command(args.file.read_text())
        save_env(updates)
        print('Configured .env. Credentials and subscriber values were not printed.')
        print('Duplicate cookie names were reduced to their last captured values.')
        print('Run python run.py doctor, then BASELINE-001 against QA.')
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print('Import failed. Check the URL, quoting and JSON body; no request was executed.', file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
