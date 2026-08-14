Given these strategic priorities for ${customer}:
${priorities}

Build a strategic value matrix that tells THIS organization's story. Each row
maps a business outcome to the capability that delivers it, naming the
enabling technology only as the enabler — never as the headline.

Return JSON with:
- "rows": array of objects, each with "outcome" (string), "capability"
  (string), and "enabler" (string)
