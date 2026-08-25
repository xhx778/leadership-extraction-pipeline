# Navigator Link Selector — Step 2: LLM link pruning
# Placeholders: {company_name}, {count}, {max_select}, {links}

You are navigating the website of "{company_name}" to find their leadership or team listing page.

Below are {count} internal links found on a page. Select up to {max_select} that are most likely to lead directly to a page listing MULTIPLE executives, board members, or senior team members.

Links (index | URL path | link text):
{links}

Rules:
- Prefer URLs that point to LIST pages showing multiple executives (e.g. Board of Directors, Management Team, Leadership Team, Our People).
- A company may have SEPARATE list pages for the board and management — include BOTH if you see them as distinct candidates.
- AVOID URLs that look like individual person profiles (e.g. /team/john-smith, /people/jane-doe, /leadership/firstname-lastname).
- AVOID: news, blog, careers, products, investor-relations sub-pages.
- **Language preference:** If equivalent URLs exist in multiple languages, prefer the English version. English URL patterns include segments like `/en/`, `/en-us/`, `/en-gb/`, `/english/`, or a path that starts with `/en` before the content segment (e.g. `/en/about/leadership`). If no English version is identifiable, include the best available candidate regardless of language.

Return ONLY a JSON array of the selected URL paths (as they appear above), no other text:
["path1", "path2", ...]
