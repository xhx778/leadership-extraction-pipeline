# Extractor Prompt
# Placeholders: {company_name}, {raw_content}

Extract ALL senior leadership members from this content about {company_name}.

Content:
{raw_content}

Return ONLY a valid JSON array — no markdown, no explanation, no other text:
[
  {{
    "name": "Full Name",
    "title": "Exact title as stated in the source"
  }}
]

Rules:
- Include every person with their exact title as written in the source.
- Only include CURRENT officeholders. Skip "Former", "Past", "Previous", "Outgoing", "Retired".
- Skip section headings, department names, navigation items, and HTML artifacts.
- If the same person appears with multiple roles, include them once with the most senior title.
- Return [] if no valid records are found.
