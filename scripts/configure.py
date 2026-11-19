#!/usr/bin/env python3
"""Prompt for private local configuration without echoing credentials."""
import getpass
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.import_request import save_env


def main():
    print('Configure the designated QA subscriber from your working Postman request.')
    print('The key, cookies and subscriber identity are entered without echo.')
    values = {
      'MORPHEUS_API_KEY':getpass.getpass('Query key: ').strip(),
      'MORPHEUS_COOKIE':getpass.getpass('Cookie header (optional; press Enter to omit): ').strip(),
      'QA_MSISDN_A':getpass.getpass('Designated QA MSISDN (international digits): ').strip(),
      'QA_CONVERSATION_ID_A':getpass.getpass('Existing valid conversation ID: ').strip(),
    }
    kind=input('subscriberType [POSTPAID]: ').strip() or 'POSTPAID'
    values['QA_SUBSCRIBER_TYPE_A']=kind
    if not values['QA_MSISDN_A'] or not values['QA_CONVERSATION_ID_A']:
        print('No configuration saved: subscriber and conversation ID are required.')
        return 2
    save_env(values)
    print('Saved .env with owner-only permissions. Run python run.py doctor.')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
