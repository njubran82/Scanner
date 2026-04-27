#!/usr/bin/env python3
"""Scan inbox for any emails from booksgoat.com and print their subject lines."""

import os, imaplib, email
from datetime import datetime, timedelta

SMTP_USER     = os.environ['SMTP_USER']
SMTP_PASSWORD = os.environ['SMTP_PASSWORD']

mail = imaplib.IMAP4_SSL('imap.gmail.com', 993)
mail.login(SMTP_USER, SMTP_PASSWORD)
mail.select('inbox')
print('✅ Connected\n')

since = (datetime.now() - timedelta(days=60)).strftime('%d-%b-%Y')
_, msg_ids = mail.search(None, 'SINCE', since, 'FROM', 'booksgoat')
ids = msg_ids[0].split()
print(f'Found {len(ids)} emails from BooksGoat in last 60 days:\n')

for msg_id in ids:
    _, msg_data = mail.fetch(msg_id, '(RFC822)')
    msg = email.message_from_bytes(msg_data[0][1])
    print(f'  Subject: {msg.get("Subject", "")}')
    print(f'  From:    {msg.get("From", "")}')
    print(f'  Date:    {msg.get("Date", "")}')
    print()

mail.logout()
