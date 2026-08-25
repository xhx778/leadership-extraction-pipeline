# Navigator Page Classifier — Step 3: LLM page confirmation
# Placeholders: {company_name}, {count}, {pages}

You are reviewing {count} candidate pages from {company_name}'s website.

The earlier BFS stage used loose Python rules to collect candidates — so this list may include false positives such as individual person profiles, news articles, or about pages that merely mention executives in passing. Your job is to identify which candidates are TRUE leadership LIST pages.

A TRUE leadership list page:
- Shows MULTIPLE executives (typically 3 or more) with their names and titles
- Is structured as a directory or listing, not a narrative or news article
- Is NOT a single person's profile, even if that person holds a senior title
- Is NOT a news article or press release that mentions executives in passing

A company may have leadership info across MULTIPLE pages (e.g. Board of Directors and Management Team as separate URLs). Include ALL pages that qualify as real leadership lists.

Candidates:
{pages}

Return ONLY valid JSON, no other text. Reference candidates by their number above:
{{
  "confirmed": [
    {{"index": <int>, "reason": "brief explanation"}}
  ]
}}

If no candidates qualify, return {{"confirmed": []}}.
