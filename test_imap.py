#!/usr/bin/env python3
"""Dry run — finds BooksGoat shipping emails, parses them, prints results."""

import os, imaplib, email, re
from email.header import decode_header as _dh
from datetime import datetime, timedelta

SMTP_USER     = os.environ['SMTP_USER']
SMTP_PASSWORD = os.environ['SMTP_PASSWORD']

def decode_header(val):
    parts = _dh(val or '')
    result = ''
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or 'utf-8', errors='ignore')
        else:
            result += part
    return result

def extract_raw_and_text(msg):
    """Return (raw_bytes, plain_text) from email."""
    raw = b''
    text = ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            payload = part.get_payload(decode=True) or b''
            raw += payload
            if ct == 'text/plain':
                text += payload.decode('utf-8', errors='ignore')
            elif ct == 'text/html':
                html = payload.decode('utf-8', errors='ignore')
                text += re.sub(r'<[^>]+>', ' ', html)
    else:
        payload = msg.get_payload(decode=True) or b''
        raw = payload
        text = payload.decode('utf-8', errors='ignore')
    return raw.decode('utf-8', errors='ignore'), text

def parse_shipping_email(text):
    result = {}
    m = re.search(r'[Oo]rder\s*(?:ID|#|No\.?)[:\s#]*(\d+)', text)
    result['order_id'] = m.group(1) if m else None
    m = re.search(r'[Tt]racking\s*[Nn]umber[:\s]*([A-Za-z0-9]{8,30})', text)
    result['tracking'] = m.group(1) if m else None
    result['carrier'] = 'FEDEX'
    if re.search(r'\bups\b', text, re.IGNORECASE): result['carrier'] = 'UPS'
    elif re.search(r'\busps\b', text, re.IGNORECASE): result['carrier'] = 'USPS'
    m = re.search(r'ISBN[:\s]*(97[89]\d{10})', text)
    result['isbn'] = m.group(1) if m else None
    # Buyer name from shipping address block
    m = re.search(r'Shipping\s+Address[:\s]*([A-Z][a-z]+\s+[A-Z][a-z]+)', text)
    result['buyer_name'] = m.group(1).strip() if m else None
    return result

print('Connecting to Gmail IMAP...')
mail = imaplib.IMAP4_SSL('imap.gmail.com', 993)
mail.login(SMTP_USER, SMTP_PASSWORD)
mail.select('inbox')
print('✅ Connected\n')

since = (datetime.now() - timedelta(days=14)).strftime('%d-%b-%Y')
_, msg_ids = mail.search(None, 'SINCE', since, 'FROM', 'booksgoat', 'SUBJECT', '"Order Update"')
ids = msg_ids[0].split()
print(f'Found {len(ids)} BooksGoat Order Update emails in last 14 days\n')

found = 0
for msg_id in ids:
    _, msg_data = mail.fetch(msg_id, '(RFC822)')
    msg = email.message_from_bytes(msg_data[0][1])
    subject = decode_header(msg.get('Subject', ''))
    raw, text = extract_raw_and_text(msg)

    # Search raw content for SHIPPED signal
    if 'SHIPPED' not in raw.upper() and 'SHIPPED' not in text.upper():
        continue

    found += 1
    parsed = parse_shipping_email(text)
    print(f'--- Shipping Email ---')
    print(f'  Subject:  {subject}')
    print(f'  Date:     {msg.get("Date", "")}')
    print(f'  Order ID: {parsed["order_id"]}')
    print(f'  ISBN:     {parsed["isbn"]}')
    print(f'  Tracking: {parsed["tracking"]}')
    print(f'  Carrier:  {parsed["carrier"]}')
    print(f'  Buyer:    {parsed["buyer_name"]}')
    print()

if found == 0:
    print('No shipping emails found — printing first email raw text for debugging:\n')
    if ids:
        _, msg_data = mail.fetch(ids[0], '(RFC822)')
        msg = email.message_from_bytes(msg_data[0][1])
        _, text = extract_raw_and_text(msg)
        print(text[:2000])

mail.logout()
print('\nDone.')
