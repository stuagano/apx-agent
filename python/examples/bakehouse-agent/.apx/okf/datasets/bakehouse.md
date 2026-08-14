---
type: Databricks Schema
title: bakehouse
description: 'Databricks'' built-in samples.bakehouse dataset: a fictional bakery''s sales (transactions, customers, franchises, suppliers) plus free-text customer reviews.'
resource: samples.bakehouse
catalog: samples
schema: bakehouse
timestamp: '2026-08-14T20:00:00+00:00'
---

# Tables
* [sales_transactions](../tables/sales_transactions.md)
* [sales_customers](../tables/sales_customers.md)
* [sales_franchises](../tables/sales_franchises.md)
* [sales_suppliers](../tables/sales_suppliers.md)
* [media_customer_reviews](../tables/media_customer_reviews.md)

# Glossary
### Revenue
Total sales value, computed as `SUM(sales_transactions.totalPrice)`. Never sum `unitPrice` (that is per-unit). Synonyms: sales, total sales, turnover, GMV.

### Franchise
A bakery outlet/location (`sales_franchises`). Sales roll up to a franchise via `sales_transactions.franchiseID`. Synonyms: location, store, outlet, shop.

### Supplier
An ingredient supplier (`sales_suppliers`) that serves one or more franchises via `sales_franchises.supplierID`. Synonyms: vendor, ingredient supplier.

### Product
An item sold, named in `sales_transactions.product` (e.g. "Golden Gate Ginger"). Synonyms: item, SKU, menu item, bake.

### Review
A customer's free-text feedback in `media_customer_reviews.review`, tied to a franchise. The basis for sentiment and theme questions. Synonyms: feedback, comment, customer voice.

### Sentiment
Whether reviews skew positive or negative — inferred by reading `media_customer_reviews.review` text (no precomputed score column). Synonyms: mood, tone, satisfaction.
