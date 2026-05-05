-- Data Triage Agent — fe-stable fixture data
-- Creates the schema + tables referenced by eval_cases.md
-- Run with: databricks api post /api/2.0/sql/statements --profile fe-stable --json @<query.json>
-- Or via Databricks SQL editor on the workspace.

-- Default catalog/schema can be overridden by setting CATALOG before running.
-- Sticking with serverless_stable_qh44kx_catalog (fe-stable workspace native catalog).

-- Variables (set via SQL widget OR substitute via sed before running)
-- CATALOG = serverless_stable_qh44kx_catalog
-- SCHEMA  = explain_my_bill

CREATE SCHEMA IF NOT EXISTS serverless_stable_qh44kx_catalog.explain_my_bill;

-- ----------------------------------------------------------------------
-- customers — 10 rows, CUST-0001..0010 (CUST-0011 deliberately missing)
-- Used by eval cases #1, #2, #3, #4, #7
-- ----------------------------------------------------------------------
CREATE OR REPLACE TABLE serverless_stable_qh44kx_catalog.explain_my_bill.customers (
  customer_id STRING,
  name STRING,
  plan STRING,
  address STRING,
  signup_date DATE,
  active BOOLEAN
) USING DELTA;

INSERT INTO serverless_stable_qh44kx_catalog.explain_my_bill.customers VALUES
  ('CUST-0001', 'Alice Johnson',  'Standard Plan',     '12 Main St',     DATE '2024-01-15', true),
  ('CUST-0002', 'Bob Martinez',   'Premium Plan',      '47 Oak Ave',     DATE '2024-02-22', true),
  ('CUST-0003', 'Priya Patel',    'Green Energy Plan', '88 Sunset Blvd', DATE '2024-03-08', true),
  ('CUST-0004', 'Diego Rivera',   'Standard Plan',     '19 Cedar Ln',    DATE '2024-03-19', true),
  ('CUST-0005', 'Emma Chen',      'Green Energy Plan', '56 Maple Dr',    DATE '2024-04-02', true),
  ('CUST-0006', 'Frank Okafor',   'Premium Plan',      '23 Pine Rd',     DATE '2024-04-11', false),
  ('CUST-0007', 'Grace Lin',      'Standard Plan',     '101 Elm St',     DATE '2024-05-07', true),
  ('CUST-0008', 'Hassan Ali',     'Green Energy Plan', '74 Birch Ct',    DATE '2024-05-21', true),
  ('CUST-0009', 'Isabel Santos',  'Standard Plan',     '38 Willow Way',  DATE '2024-06-15', true),
  ('CUST-0010', 'Jamal Brooks',   'Premium Plan',      '92 Spruce Ave',  DATE '2024-07-03', true);

-- ----------------------------------------------------------------------
-- billing_history — has a deliberate March 2026 gap for eval #10
-- ----------------------------------------------------------------------
CREATE OR REPLACE TABLE serverless_stable_qh44kx_catalog.explain_my_bill.billing_history (
  bill_id STRING,
  customer_id STRING,
  bill_month DATE,
  amount_usd DECIMAL(10,2),
  status STRING
) USING DELTA;

-- Generate billing data Jan 2026 - Apr 2026, but skip March 2026 entirely (data gap)
INSERT INTO serverless_stable_qh44kx_catalog.explain_my_bill.billing_history
SELECT
  concat('BILL-', cast(row_number() OVER (ORDER BY c.customer_id, m.bill_month) AS STRING)) AS bill_id,
  c.customer_id,
  m.bill_month,
  CAST(75 + rand() * 200 AS DECIMAL(10,2)) AS amount_usd,
  'paid' AS status
FROM serverless_stable_qh44kx_catalog.explain_my_bill.customers c
CROSS JOIN (
  SELECT explode(array(DATE '2026-01-01', DATE '2026-02-01', DATE '2026-04-01')) AS bill_month
) m
WHERE c.active = true;

-- ----------------------------------------------------------------------
-- ami_hourly_rollups — STALE: latest data is from Feb 2026 (eval #9)
-- ----------------------------------------------------------------------
CREATE OR REPLACE TABLE serverless_stable_qh44kx_catalog.explain_my_bill.ami_hourly_rollups (
  customer_id STRING,
  meter_id STRING,
  hour_ts TIMESTAMP,
  kwh DECIMAL(10,3),
  ingested_at TIMESTAMP
) USING DELTA;

INSERT INTO serverless_stable_qh44kx_catalog.explain_my_bill.ami_hourly_rollups
SELECT
  c.customer_id,
  concat('METER-', c.customer_id) AS meter_id,
  hour_ts,
  CAST(0.1 + rand() * 3.0 AS DECIMAL(10,3)) AS kwh,
  TIMESTAMP '2026-02-28 23:59:00' AS ingested_at
FROM serverless_stable_qh44kx_catalog.explain_my_bill.customers c
CROSS JOIN (
  SELECT TIMESTAMP '2026-02-15 00:00:00' + (n * INTERVAL '1' HOUR) AS hour_ts
  FROM (SELECT explode(sequence(0, 23)) AS n)
) hours
WHERE c.active = true;

-- ----------------------------------------------------------------------
-- Verify
-- ----------------------------------------------------------------------
SELECT 'customers' AS tbl, COUNT(*) AS n FROM serverless_stable_qh44kx_catalog.explain_my_bill.customers
UNION ALL SELECT 'billing_history', COUNT(*) FROM serverless_stable_qh44kx_catalog.explain_my_bill.billing_history
UNION ALL SELECT 'ami_hourly_rollups', COUNT(*) FROM serverless_stable_qh44kx_catalog.explain_my_bill.ami_hourly_rollups;
