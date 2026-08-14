---
type: Databricks Schema
title: precall
description: 'Pre-call brief data: 7 governed views + 7 section functions, keyed by
  company, for building a rep''s pre-call brief.'
resource: main.precall
catalog: main
schema: precall
timestamp: '2026-08-14T19:45:08.067955+00:00'
---

# Tables
* [vw_orders](../tables/vw_orders.md)
* [vw_opportunities](../tables/vw_opportunities.md)
* [vw_winloss](../tables/vw_winloss.md)
* [vw_rmas](../tables/vw_rmas.md)
* [vw_pprs](../tables/vw_pprs.md)
* [vw_field_notes](../tables/vw_field_notes.md)
* [vw_actions](../tables/vw_actions.md)

# Functions
* [open_orders_and_shipping](../functions/open_orders_and_shipping.md)
* [open_opportunities](../functions/open_opportunities.md)
* [recent_win_loss](../functions/recent_win_loss.md)
* [open_rmas](../functions/open_rmas.md)
* [open_pprs](../functions/open_pprs.md)
* [field_notes](../functions/field_notes.md)
* [overdue_actions](../functions/overdue_actions.md)

# Glossary
### Pre-Call Brief
A one-page summary a rep reads before a customer visit, assembled from 7 sections (orders, opportunities, win/loss, RMAs, PPRs, field notes, overdue actions). Synonyms: call brief, pre-visit brief, briefing.

### RMA
Return Merchandise Authorization — an approved return or repair, tracked in your field-service system. Synonyms: return, return authorization.

### PPR
Product Problem Report — a logged product-quality issue, similar to a CAPA. Severity ranges Low to Critical. Synonyms: product problem report, CAPA, quality issue.

