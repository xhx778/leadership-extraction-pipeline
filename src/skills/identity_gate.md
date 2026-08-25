# Identity Gate Prompt (sent to DeepSeek)
# Placeholders: {company}, {identity_text}

You are verifying whether a website is the official website of a specific company.

Company legal name: {company}

Identity zones extracted from the candidate site's homepage
(title / footer copyright / about-contact anchors):
{identity_text}

Decide whether this website is the official website of THIS SPECIFIC COMPANY itself.

Answer YES if EITHER of the following holds:
  1. The entity the site identifies itself as IS this company (exact or allowed-variant match).
  2. The site is the parent company or global group website and shares the SAME core brand
     name as the target local subsidiary. Example: target is "DEF Securities Pte Ltd"
     (Singapore), site identifies as "DEF Securities AS" (Norwegian parent) — core brand
     "DEF Securities" is identical, and "AS" / "Pte Ltd" are merely jurisdiction-specific
     legal suffixes. The group website IS the authoritative official website for the
     subsidiary when no separate local site exists. Answer YES in this case.
     This override applies ONLY when the core brand identifier is the same; a genuinely
     different parent brand (e.g. site says "ABC Holdings" for target "GHI Partners Pte Ltd")
     is still NO.

**Language priority — use English name when available:**
The target company name is always in English. If the identity zones contain BOTH
an English name and a non-English name (Chinese, Japanese, Korean, etc.), judge
entity match using the English name only — ignore the non-English name for
matching purposes. Only fall back to non-English names when no English company
name is present anywhere in the identity zones.

**Name matching — what is and is not allowed:**
Official sites routinely display only a brand name or short name, not the full
legal name. Apply these rules when comparing the site's self-reported name against
the company legal name:

- ALLOWED variants (same entity, answer YES):
  · Legal suffixes absent or different: "GHI Partners" or "GHI Partners Limited"
    matches "GHI Partners Management Pte. Limited" — the core identifier "GHI Partners"
    is the same entity; MANAGEMENT / PTE / LTD / LIMITED being absent is not evidence
    of a different company.
  · & vs and, abbreviations, capitalisation differences, presence/absence of
    Pte / Ltd / Limited / Holdings / Corp / Inc.
  · Brand name or trading name used instead of full legal name, as long as the
    core identifier points to the same entity.
  · Parent company or global group website standing in for a local subsidiary:
    if the target company is a local subsidiary (e.g. "DEF Securities Pte Ltd"
    or "XYZ Capital Hong Kong Limited") and the site identifies as the parent or
    global group entity with the SAME core brand name (e.g. "DEF Securities AS",
    "XYZ Capital Group"), answer YES — the group website is the authoritative
    official source for the subsidiary when no separate local site exists.
    This applies only when the core brand identifier matches; a genuinely different
    parent brand (e.g. site says "ABC Holdings" for target "GHI Partners Pte Ltd")
    is still NO.

- NOT allowed (different entity, answer NO):
  · The core brand identifier itself does not match — e.g. the site says
    "ABC Partners Management" but the target is "GHI Partners Management Pte. Limited":
    the two names DO overlap, on "Partners Management" — but that entire overlap is
    generic sector words, and the parts that actually identify the firm, "ABC" vs "GHI",
    are different. So these are different entities. This is the exact mirror of the
    ALLOWED case above: there the identifying part was the same and only legal suffixes
    differed (YES); here the legal suffixes and generic words line up but the identifying
    part differs (NO). Overlap that is entirely generic proves nothing.
  · Generic sector words (investment / capital / asia / group / holdings / management /
    partners / securities) appearing in both names does NOT make them the same entity.
Answer NO if ANY of the following holds:
  (a) it is a different company, a directory site, a news site, an aggregator,
      or merely topically related. Generic words in the name (investment /
      capital / asia / group / holdings, etc.) being topically related DOES NOT
      COUNT — it must be the same entity.
      EXCEPTION: a parent or group company website with the SAME core brand name
      as the target subsidiary is NOT "a different company" — do NOT apply (a)
      in that case; apply the YES rule above instead.
  (b) the page is NOT a usable official site at all: a blank / near-empty page,
      a parked or for-sale domain, a "coming soon" / under-construction splash,
      a soft 404 / error page, or any page with no real company content.
      In this case set matched_entity to "" and say so in reason.
  (c) the page is a social media or professional network platform — LinkedIn,
      Facebook, Instagram, Twitter/X, YouTube, or similar. Even if the company's
      name appears prominently in the title or footer copyright, a social profile
      is NOT the company's own site. Signs: copyright or branding by LinkedIn /
      Meta / X Corp, login walls, follower/employee counts in the content.
Answer UNSURE if: the page DOES have real content but the identity zones are
  insufficient to confirm or deny the entity (e.g. only a single brand word,
  missing footer). UNSURE is a fully acceptable answer. Do NOT force a YES just
  to deliver something. Do NOT use UNSURE for blank/abnormal pages — those are NO.

**Scope of judgment — entity attribution only, not page quality:**
Your only task is to decide whether this site belongs to this company. Do NOT
answer NO or UNSURE because of page quality issues such as broken links, truncated
URL fragments (e.g. "rs.com/about-us/"), garbled text, layout problems, or
incomplete rendering — these are irrelevant to entity attribution. Only judge NO
or UNSURE when the identity evidence itself points to a different company, or is
genuinely insufficient to confirm the entity.

Output JSON only, no other text:
{{"verdict": "YES" | "NO" | "UNSURE", "reason": "<one-sentence basis>", "matched_entity": "<entity name the site identifies as, empty string if none>"}}
