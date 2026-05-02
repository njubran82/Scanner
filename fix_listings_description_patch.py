"""
=============================================================
  fix_listings_description_patch.py
  Built   : 2026-05-02
=============================================================

  INSTRUCTIONS
  ------------
  In E:/Book/Lister/fix_listings.py, find the existing
  generate_description() function and REPLACE IT entirely
  with the version below.

  WHAT CHANGED
  ------------
  - Old prompt: "Write a 2-3 sentence eBay listing description..."
    (too vague — Haiku regurgitates publisher marketing copy)

  - New prompt: Strict 10-rule constraint that forces:
    * 2-3 sentences, 40-80 words max
    * No bullet points, no feature lists
    * No repeated phrases or ideas
    * No marketing buzzwords
    * Post-processing to catch and fix violations

  FIND THIS IN fix_listings.py:
  ─────────────────────────────
  def generate_description(title: str, isbn: str) -> str:
      ...everything through the end of the function...

  REPLACE WITH EVERYTHING BELOW:
  ─────────────────────────────
"""


def generate_description(title: str, isbn: str) -> str:
    """
    Generate a concise, non-repetitive eBay listing description
    using Claude Haiku. Falls back to plain title + ISBN if the
    API key is missing or the call fails.
    """
    if not ANTHROPIC_API_KEY:
        return f"{title}\n\nISBN: {isbn}\n\n{DISCLAIMER}"

    # ── Pre-process: strip publisher marketing from the title ──
    # This prevents Haiku from echoing back the same blurbs
    clean_title = title
    for suffix in [
        " - Comprehensive", " - A Complete", " - The Definitive",
        " - Your Complete", " - The Essential", " - An Introduction",
    ]:
        if suffix in clean_title:
            clean_title = clean_title.split(suffix)[0]
            break
    clean_title = clean_title[:120]

    # ── Constrained prompt ──────────────────────────────────────
    prompt = f"""Write an eBay listing description for this textbook.

Title: {clean_title}
ISBN: {isbn}

STRICT RULES — violating any rule means the description is rejected:
1. Exactly 2–3 sentences. No more.
2. First sentence: what the book covers and who it's for.
3. Second sentence: one standout feature (e.g., edition highlights, page count, practice questions).
4. Optional third sentence: only if genuinely adding new info.
5. DO NOT list chapter names, division names, or feature bullet points.
6. DO NOT repeat any phrase or idea — every sentence must say something new.
7. DO NOT use marketing language like "must-have", "comprehensive", "essential", "trusted".
8. DO NOT mention price, condition, shipping, or seller info.
9. Plain text only. No bullet points, no headers, no special formatting.
10. Total length: 40–80 words maximum.

Write the description now:"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        desc = msg.content[0].text.strip()

        # ── Post-processing ─────────────────────────────────────
        # Strip quotes and preamble Haiku sometimes adds
        desc = desc.replace('"', '').strip('" \n')
        for prefix in ["Here's", "Here is", "Sure,", "Sure!"]:
            if desc.startswith(prefix):
                desc = desc[len(prefix):].lstrip(" :,\n")

        # Truncate if too long
        word_count = len(desc.split())
        if word_count > 100:
            sentences = desc.split(". ")
            desc = ". ".join(sentences[:3])
            if not desc.endswith("."):
                desc += "."
            log.warning(f"  Description was {word_count} words — truncated to 3 sentences")

        # Detect repetition: if two sentences share >50% of words, keep only the first
        sentences = [s.strip() for s in desc.split(". ") if s.strip()]
        if len(sentences) >= 2:
            clean_sentences = [sentences[0]]
            for k in range(1, len(sentences)):
                words_prev = set(sentences[k - 1].lower().split())
                words_curr = set(sentences[k].lower().split())
                if len(words_prev) > 3 and len(words_curr) > 3:
                    overlap = len(words_prev & words_curr) / min(len(words_prev), len(words_curr))
                    if overlap > 0.5:
                        log.warning(f"  Repetition detected ({overlap:.0%}) — dropping sentence {k + 1}")
                        continue
                clean_sentences.append(sentences[k])
            desc = ". ".join(clean_sentences)
            if not desc.endswith("."):
                desc += "."

        return f"{desc}\n\nISBN: {isbn}\n\n{DISCLAIMER}"

    except Exception as e:
        log.warning(f"Claude API error for {isbn}: {e}")
        return f"{title}\n\nISBN: {isbn}\n\n{DISCLAIMER}"
