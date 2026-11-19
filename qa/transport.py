"""Bounded HTTPS transport and SSE framing. No retry of non-idempotent POSTs."""
import codecs
import hashlib
import http.client
import json
import socket
import ssl
import time
import os
from urllib.parse import urlsplit

def get_path(obj, path):
    for part in path.split('.'):
        try:
            obj = obj[int(part)] if isinstance(obj, list) else obj[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return obj

class SSEParser:
    def __init__(self):
        self.buffer = ''
        self.event = {}
        self.data = []
        self.events = []

    def line(self, line):
        if line == '':
            if self.data:
                self.events.append({**self.event, 'data': '\n'.join(self.data)})
            self.event, self.data = {}, []
            return
        if line.startswith(':'):
            return
        field, sep, value = line.partition(':')
        if sep and value.startswith(' '):
            value = value[1:]
        if field == 'data':
            self.data.append(value)
        elif field in ('event', 'id'):
            self.event[field] = value

    def feed(self, text):
        self.buffer += text
        # Supports LF, CRLF and bare CR, including a CR split across reads.
        while self.buffer:
            positions = [p for p in (self.buffer.find('\n'), self.buffer.find('\r')) if p >= 0]
            if not positions:
                break
            p = min(positions)
            if self.buffer[p] == '\r' and p + 1 == len(self.buffer):
                break
            n = 2 if self.buffer[p:p+2] == '\r\n' else 1
            self.line(self.buffer[:p])
            self.buffer = self.buffer[p+n:]

    def finish(self):
        if self.buffer.endswith('\r'):
            self.feed('\n')
        # SSE spec does not dispatch an unterminated event at EOF.
        return self.events

def extract_answer(events, config):
    answer = ''
    for event in events:
        data = event.get('data', '')
        if data == '[DONE]':
            continue
        try:
            item = json.loads(data)
        except json.JSONDecodeError:
            # Only explicit text events are extracted; raw event data remains available.
            if event.get('event') in ('token', 'delta'):
                answer += data
            elif event.get('event') in ('answer', 'final', 'message'):
                answer = data
            continue
        if isinstance(item, str):
            answer += item
            continue
        found = False
        for path in config.get('delta_paths', []):
            value = get_path(item, path)
            if isinstance(value, str):
                answer += value
                found = True
                break
        if not found:
            for path in config.get('snapshot_paths', []):
                value = get_path(item, path)
                if isinstance(value, str):
                    answer = value
                    break
    return answer

def request(url, method, headers, body, config):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in config['allowed_hosts']:
        raise ValueError('Target must use HTTPS and match config.allowed_hosts exactly.')
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('URL credentials and fragments are unsupported.')
    start = time.monotonic()
    result = {'status_code': None, 'headers': {}, 'raw_body': '', 'events': [],
              'answer': '', 'first_byte_ms': None, 'first_event_ms': None,
              'elapsed_ms': None, 'complete': False, 'error': None, 'done_received': False}
    raw = bytearray()
    parser = SSEParser()
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    # Trust an optional enterprise CA bundle without disabling certificate verification.
    context = ssl.create_default_context(cafile=os.environ.get('MORPHEUS_CA_FILE') or None)
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
        timeout=min(config['read_timeout_seconds'], config['timeout_seconds']), context=context)
    try:
        conn.request(method, parsed.path + ('?' + parsed.query if parsed.query else ''),
                     body=body, headers={**{k.lower(): v for k, v in headers.items()}, 'accept-encoding': 'identity'})
        response = conn.getresponse()
        result['status_code'] = response.status
        result['headers'] = {k.lower(): v for k,v in response.getheaders()}
        if method == 'HEAD':
            result['complete'] = True
        else:
            while True:
                left = config['timeout_seconds'] - (time.monotonic() - start)
                if left <= 0:
                    result['error'] = 'total_timeout'
                    break
                remaining = config['max_response_bytes'] - len(raw)
                if remaining <= 0:
                    result['error'] = 'response_size_limit'
                    break
                if conn.sock:
                    conn.sock.settimeout(min(config['read_timeout_seconds'], left))
                chunk = response.read1(min(4096, remaining))
                if not chunk:
                    result['complete'] = True
                    break
                now = round((time.monotonic() - start) * 1000, 3)
                if result['first_byte_ms'] is None:
                    result['first_byte_ms'] = now
                raw.extend(chunk)
                parser.feed(decoder.decode(chunk))
                if parser.events and result['first_event_ms'] is None:
                    result['first_event_ms'] = now
                done = any(e.get('data') == '[DONE]' or e.get('event') in config.get('done_event_names', []) for e in parser.events)
                if done:
                    result['complete'] = True
                    result['done_received'] = True
                    break
    except (OSError, http.client.HTTPException, socket.timeout) as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        conn.close()
        result['elapsed_ms'] = round((time.monotonic() - start) * 1000, 3)
        result['raw_body'] = raw.decode('utf-8', errors='replace')
        result['response_sha256'] = hashlib.sha256(raw).hexdigest()
        result['events'] = parser.finish()
        result['answer'] = extract_answer(result['events'], config)
    return result
