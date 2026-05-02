# Email Template Integration Guide
# =================================
# Date: 05/01/2026
#
# STEP 1: Deploy email_helpers.py to repo root
#   Copy email_helpers.py to E:\Book\Scanner\ (repo root)
#   Then: git add email_helpers.py && git commit -m "Add shared HTML email templates" && git push
#
# STEP 2: Apply patches below to each script.
#   Each patch shows the OLD code to find and the NEW code to replace it with.
#   Business logic is UNTOUCHED — only email formatting changes.


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH 1: lister.py
# ═══════════════════════════════════════════════════════════════════════════════
#
# A) Add import at top of file (near other imports):
#
#   from email_helpers import build_lister_success_email, build_lister_failure_email, send_html_email
#
# B) Find and replace the send_alerts function.
#
# ── OLD (find this): ──────────────────────────────────────────────────────────

def send_alerts(listed_books):
    subject = f"[Lister] {len(listed_books)} new books listed on eBay"
    lines = [
        f"- {b['title']} | ${b['listing_price']:.2f} | Profit: ${b['profit']:.2f} | {b['confidence']}"
        for b in listed_books
    ]
    body = "New listings added:\n\n" + "\n".join(lines)

    if not all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.warning("Email not configured — skipping alert")
        return
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From']    = EMAIL_FROM or SMTP_USER
        msg['To']      = EMAIL_TO
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(msg['From'], [EMAIL_TO], msg.as_string())
        log.info("Email alert sent")
    except Exception as e:
        log.error(f"Email alert failed: {e}")

# ── NEW (replace with): ──────────────────────────────────────────────────────

def send_alerts(listed_books):
    if not listed_books:
        return
    subject = f"[Lister] ✅ {len(listed_books)} new books listed on eBay"
    html = build_lister_success_email(listed_books)
    send_html_email(subject, html)


def send_failure_alert(failed_books):
    """Call this wherever you currently log listing failures."""
    if not failed_books:
        return
    subject = f"[Lister] 🔴 {len(failed_books)} books FAILED to list"
    html = build_lister_failure_email(failed_books)
    send_html_email(subject, html)

# ── C) Wire send_failure_alert into the listing loop: ─────────────────────────
#
# Find the spot where failed books are logged (look for "Failure email sent"
# or where failed_books list is populated). Add this call after the loop:
#
#   send_failure_alert(failed_books)
#
# Each entry in failed_books should be a dict with keys:
#   isbn, title, listing_price, profit, stage, error, action
#
# Example:
#   failed_books.append({
#       "isbn": isbn, "title": title,
#       "listing_price": price, "profit": profit,
#       "stage": "image",
#       "error": "No image found — all sources empty",
#       "action": "Upload photo in Seller Hub"
#   })


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH 2: fix_images.py
# ═══════════════════════════════════════════════════════════════════════════════
#
# A) Add import at top:
#
#   from email_helpers import build_fix_images_email, send_html_email
#
# B) Find and replace the email block at end of run().
#
# ── OLD (find this): ──────────────────────────────────────────────────────────

    # Send report email
    if upgraded or still_missing:
        lines = [
            f'FIX IMAGES REPORT — {datetime.now():%Y-%m-%d %H:%M}',
            f'Upgraded: {len(upgraded)} | Still missing: {len(still_missing)}',
            '=' * 50, '',
        ]
        if upgraded:
            lines.append(f'UPGRADED TO FULL QUALITY ({len(upgraded)}):')
            for u in upgraded:
                lines.append(f'  {u["isbn"]} — {u["title"]}')
            lines.append('')
        if still_missing:
            lines.append('STILL NEED MANUAL PHOTO:')
            for m in still_missing:
                lines.append(f'  {m["isbn"]} — {m["title"]} (was: {m["flag"]})')
            lines.append('')

        msg = MIMEText('\n'.join(lines))
        msg['Subject'] = f'[fix_images] {len(upgraded)} upgraded, {len(still_missing)} still missing'
        msg['From'] = EMAIL_FROM or SMTP_USER
        msg['To'] = EMAIL_TO
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.sendmail(msg['From'], [EMAIL_TO], msg.as_string())
            log.info('Report email sent')
        except Exception as e:
            log.error(f'Email failed: {e}')

# ── NEW (replace with): ──────────────────────────────────────────────────────

    # Send report email
    if upgraded or still_missing:
        subject = f'[fix_images] {len(upgraded)} upgraded, {len(still_missing)} still missing'
        html = build_fix_images_email(upgraded, still_missing)
        send_html_email(subject, html)


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH 3: fix_descriptions.py
# ═══════════════════════════════════════════════════════════════════════════════
#
# A) Add import at top:
#
#   from email_helpers import build_fix_descriptions_email, send_html_email
#
# B) Find and replace the email block at end of run().
#
# ── OLD (find this): ──────────────────────────────────────────────────────────

    # Send report email
    if updated or failed:
        lines = [
            f'DESCRIPTION FIX REPORT — {datetime.now():%Y-%m-%d %H:%M}',
            f'Updated: {len(updated)} | Failed: {len(failed)}',
            '=' * 50, '',
        ]
        if updated:
            lines.append(f'DESCRIPTIONS ADDED ({len(updated)}):')
            for u in updated:
                lines.append(f'  {u["isbn"]} — {u["title"]}')
            lines.append('')
        if failed:
            lines.append(f'EBAY UPDATE FAILED ({len(failed)}):')
            for f_item in failed:
                lines.append(f'  {f_item["isbn"]} — {f_item["title"]}')
            lines.append('')

        msg = MIMEText('\n'.join(lines))
        msg['Subject'] = f'[fix_descriptions] {len(updated)} added, {len(failed)} failed'
        msg['From'] = EMAIL_FROM or SMTP_USER
        msg['To'] = EMAIL_TO
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.sendmail(msg['From'], [EMAIL_TO], msg.as_string())
            log.info('Report email sent')
        except Exception as e:
            log.error(f'Email failed: {e}')

# ── NEW (replace with): ──────────────────────────────────────────────────────

    # Send report email
    if updated or failed:
        subject = f'[fix_descriptions] {len(updated)} added, {len(failed)} failed'
        html = build_fix_descriptions_email(updated, failed)
        send_html_email(subject, html)


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH 4: shipping_tracker.py
# ═══════════════════════════════════════════════════════════════════════════════
#
# A) Add import at top:
#
#   from email_helpers import build_shipping_tracker_email, send_html_email
#
# B) Find the email send block at the end of the main function.
#    The current code builds a plain-text summary and sends via MIMEText.
#    Replace the email section with:
#
# ── NEW: ──────────────────────────────────────────────────────────────────────

    # Send report email
    if shipped or skipped or failed:
        subject = f'[shipping_tracker] {len(shipped)} shipped, {len(skipped)} skipped, {len(failed)} failed'
        html = build_shipping_tracker_email(shipped, skipped, failed, TRACKING_ENABLED)
        send_html_email(subject, html)

#
# Each list entry should be a dict:
#   shipped:  {isbn, title, order_id, tracking}
#   skipped:  {isbn, title, reason}
#   failed:   {isbn, title, order_id, error}
#
# TRACKING_ENABLED flag is preserved and shown prominently in the email header.


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH 5: weekly_summary.py
# ═══════════════════════════════════════════════════════════════════════════════
#
# A) Add import at top:
#
#   from email_helpers import build_weekly_summary_email, send_html_email_with_attachment
#
# B) Replace the email body builder + send block.
#    Keep the .txt attachment builder (detail text) as-is.
#    Replace only the email construction and send.
#
# ── OLD pattern (find the section that builds body + sends): ──────────────────
#
#   body = ...  (plain text with ==== dividers)
#   msg = MIMEText(body) or MIMEMultipart(...)
#   ... send via SMTP ...
#
# ── NEW (replace with): ──────────────────────────────────────────────────────

    # Build HTML body
    html_body = build_weekly_summary_email(
        active_count=active_count,
        pending_count=pending_count,
        total_csv=total_csv,
        opportunities=opportunities,
        repriced=repriced,
        delisted=delisted,
        scanner_errors=scanner_errors,
        lister_errors=lister_errors,
        repricer_errors=repricer_errors,
    )

    # detail_text = ... (keep your existing .txt attachment builder unchanged)

    run_date = datetime.now().strftime("%Y-%m-%d")
    subject = f"[Weekly Summary] Pipeline Report — {run_date}"
    send_html_email_with_attachment(
        subject, html_body,
        attachment_text=detail,              # your existing detail text variable
        attachment_filename=f"pipeline_detail_{run_date}.txt"
    )

#
# The workflow reference table is now embedded in the HTML body automatically.
# You can remove the old workflow reference block from the .txt attachment
# builder if you don't want it duplicated there.


# ═══════════════════════════════════════════════════════════════════════════════
# DEPLOYMENT CHECKLIST
# ═══════════════════════════════════════════════════════════════════════════════
#
# 1. Copy email_helpers.py to E:\Book\Scanner\ (repo root)
# 2. Apply patches 1-5 above to each script
# 3. Test locally:
#      python -c "from email_helpers import build_lister_failure_email; print('OK')"
# 4. Push to GitHub:
#      git add email_helpers.py lister.py fix_images.py fix_descriptions.py shipping_tracker.py weekly_summary.py
#      git commit -m "HTML email templates across all pipeline scripts"
#      git push
# 5. Trigger one workflow manually to verify email renders correctly
#
# No business logic is changed. Only email formatting functions are replaced.
# All SMTP env vars remain the same.
