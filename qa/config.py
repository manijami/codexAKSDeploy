"""Local configuration. .env is data, never sourced as shell code."""
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


def merge(a, b):
    result = dict(a)
    for key, value in b.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def load(root=ROOT):
    config = json.loads((root / 'config/settings.json').read_text())
    local = root / 'config/local.json'
    if local.exists():
        config = merge(config, json.loads(local.read_text()))
    env = {}
    path = root / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
                raise ValueError('Invalid .env line. Use literal NAME=value syntax.')
            env[key.strip()] = value
    env.update({k: v for k, v in os.environ.items() if k.startswith(('MORPHEUS_', 'QA_'))})
    return config, env


def target(config, env, include_key=True, account='A'):
    base = env.get('MORPHEUS_BASE_URL', 'https://qa-serviceauth.mtn.co.za').rstrip('/')
    url = urlsplit(base + config['endpoint'])
    query = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if k != 'key']
    api_key = env.get('QA_API_KEY_' + account) or env.get('MORPHEUS_API_KEY')
    if include_key and api_key:
        query.append(('key', api_key))
    return urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ''))


def request_headers(env, account='A', extra=None):
    headers = {'Accept': 'text/event-stream', 'Content-Type': 'application/json',
               'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
               'Origin': 'https://qa-serviceauth.mtn.co.za',
               'Referer': 'https://qa-serviceauth.mtn.co.za/morpheus-v5/?key='}
    if env.get('MORPHEUS_COOKIE'):
        headers['Cookie'] = env['MORPHEUS_COOKIE']
    headers = {k.lower(): v for k, v in headers.items()}
    for additions in (json.loads(env.get('MORPHEUS_HEADERS_JSON', '{}')), json.loads(env.get(f'QA_HEADERS_{account}_JSON', '{}')), extra or {}):
        headers.update({k.lower(): v for k, v in additions.items()})
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError('All header values must be strings.')
    return headers


def secrets(env):
    values = []
    for k, v in env.items():
        if any(x in k for x in ('KEY', 'COOKIE', 'MSISDN', 'CONVERSATION', 'SECRET', 'TOKEN')):
            values.append(v)
        if k.endswith('_JSON'):
            try:
                parsed = json.loads(v)
                values.extend(str(x) for x in (parsed.values() if isinstance(parsed, dict) else parsed))
            except (ValueError, TypeError):
                pass
    return [x for x in values if isinstance(x, str) and len(x) >= 4]
