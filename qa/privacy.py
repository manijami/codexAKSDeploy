"""Redact persisted reports; original response digest is retained for evidence binding."""
import re

SENSITIVE = re.compile(r'authorization|cookie|api.?key|token|password|secret', re.I)
IDENTITY = re.compile(r'^(msisdn|conversationId|conversation_id|x-conversation-id|puk1?|puk2|otp|pin|iccid|idnumber|identitynumber|email|address)$', re.I)

def redact(value, secrets=(), key=''):
    if SENSITIVE.search(key) or IDENTITY.match(key):
        return '[REDACTED]' if value else value
    if isinstance(value, dict):
        return {k: redact(v, secrets, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    for secret in sorted({s for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True):
        value = value.replace(secret, '[REDACTED]')
    value = re.sub(r'(?i)(bearer\s+)\S+', r'\1[REDACTED]', value)
    value = re.sub(r'(?i)((?:puk[12]?|otp|pin|iccid|id\s*number)\s*(?:is|:|=)?\s*)\d[\d\s-]{3,}', r'\1[REDACTED]', value)
    value = re.sub(r'(?<![\w])\+?\d(?:[ -]?\d){5,}(?![\w])', '[REDACTED-NUMBER]', value)
    value = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[REDACTED-EMAIL]', value)
    value = re.sub(r'(?i)("(?:conversationId|conversation_id|msisdn)"\s*:\s*")[^"]*(")', r'\1[REDACTED]\2', value)
    value = re.sub(r'(?i)([?&](?:key|token|sig|signature|code)=)[^\s&"<>]+', r'\1[REDACTED]', value)
    return value
