"""Campaign export redaction: preserve prices/units, remove identities and secrets."""
import re
import json
from .privacy import IDENTITY,SENSITIVE
# Public support channels quoted in the local business sources. Private QA
# values are still removed first even if they happen to match a public contact.
PUBLIC_PHONES={'0839000349','0831234273','0833857743','0831230011'}
PUBLIC_EMAILS={'coverage@mtn.com','momo@mtn.com','mnppst@mtn.co.za'}


def sanitize(value,private=(),key=''):
    if key.endswith('sha256') and isinstance(value,str) and re.fullmatch(r'[0-9a-f]{64}',value):return value
    if SENSITIVE.search(key) and key not in ('max_tokens',):return '[REDACTED]' if value else value
    if re.fullmatch(r'(?:card_?(?:number|pan|cvv|cvc)|account_?number|billing_?account(?:_?number)?|id_?number|first_?name|last_?name|full_?name)',key,re.I):return '[REDACTED]' if value else value
    if IDENTITY.match(key):return '[REDACTED]' if value else value
    if isinstance(value,dict):
        return {k:sanitize(v,private,k) for k,v in value.items() if k not in ('reasoning','reasoning_content','analysis','chain_of_thought')}
    if isinstance(value,list):return [sanitize(v,private) for v in value]
    if not isinstance(value,str):return value
    for secret in sorted({s for s in private if isinstance(s,str) and len(s)>=4},key=len,reverse=True):value=value.replace(secret,'[REDACTED]')
    if value.lstrip().startswith(('{','[')):
        try:return json.dumps(sanitize(json.loads(value),private),ensure_ascii=False)
        except (ValueError,TypeError):pass
    if any(line.startswith('data: ') for line in value.splitlines()):
        value='\n'.join('data: '+sanitize(line[6:],private) if line.startswith('data: ') and line[6:].lstrip().startswith(('{','[')) else line for line in value.split('\n'))
    value=re.sub(r'(?i)(bearer\s+)\S+',r'\1[REDACTED]',value)
    value=re.sub(r'(?i)([?&](?:key|token|sig|signature|code)=)[^\s&"<>]+',r'\1[REDACTED]',value)
    value=re.sub(r'(?i)("(?:conversationId|conversation_id|msisdn|puk[12]?|otp|pin|iccid|idnumber)"\s*:\s*")[^"]*(")',r'\1[REDACTED]\2',value)
    value=re.sub(r'(?i)((?:puk[12]?|otp|pin|iccid|id\s*number)\s*(?:is|:|=)\s*)\d(?:[\d -]{2,}\d)',r'\1[REDACTED]',value)
    value=re.sub(r'''(?i)(["']?(?:puk[12]?|otp|pin|iccid|id\s*number|account\s*number)["']?\s*(?:is|:|=)\s*["']?)\d(?:[\d -]{2,}\d)''',r'\1[REDACTED]',value)
    # Unknown local mobile numbers and email addresses may be subscriber data.
    value=re.sub(r'(?<![\w])(?:\+27[ -]?[6-8]|0[6-8])(?:[ -]?\d){8}(?!\d)',lambda m:m.group(0) if re.sub(r'\D','',m.group(0)) in PUBLIC_PHONES else '[REDACTED-PHONE]',value)
    value=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',lambda m:m.group(0) if m.group(0).lower() in PUBLIC_EMAILS else '[REDACTED-EMAIL]',value)
    value=re.sub(r'(?<![\w])\d(?:[ -]?\d){12,18}(?![\w])','[REDACTED-LONG-ID]',value)
    value=re.sub(r'\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b','[REDACTED-SESSION]',value)
    return value
