-- OBO grants for the Pre-Call Brief agent.
--
-- The agent runs each section's UC function AS THE CALLING REP (on-behalf-of),
-- so Unity Catalog decides what each rep can see. Two layers:
--   (1) BASE CHAIN — the minimum grants a rep needs to run the app at all.
--   (2) ROW-LEVEL SCOPING (production) — so a rep sees ONLY their accounts.
--
-- Replace :catalog / :schema / :reps_group before running. On the fevm-hvhhmh
-- sandbox this was applied to `account users` for demo testability (any FE who
-- logs in can try it); a real deployment grants a scoped reps group instead.

-- =====================================================================
-- (1) BASE CHAIN  — required for OBO to work
-- =====================================================================
-- Catalog + schema traversal
GRANT USE CATALOG ON CATALOG :catalog TO `:reps_group`;
GRANT USE SCHEMA  ON SCHEMA  :catalog.:schema TO `:reps_group`;

-- EXECUTE covers the 7 section functions the agent calls as tools.
GRANT EXECUTE ON SCHEMA :catalog.:schema TO `:reps_group`;

-- SELECT covers the 7 views AND their backing tables. A UC SQL function runs
-- with the INVOKER's privileges, so the rep needs SELECT down the whole chain
-- (function -> view -> src table), not just EXECUTE on the function.
GRANT SELECT ON SCHEMA :catalog.:schema TO `:reps_group`;

-- =====================================================================
-- (2) ROW-LEVEL SCOPING (production) — each rep sees only their accounts
-- =====================================================================
-- Blanket SELECT above lets every rep read every company. In production, add a
-- rep -> account mapping and a row filter so the SAME app, unchanged, scopes per
-- rep. This is the real payoff of OBO: no per-rep app logic, UC enforces it.
--
--   -- a) mapping: which companies each rep owns (populate from SFDC ownership)
--   CREATE TABLE IF NOT EXISTS :catalog.:schema.rep_accounts (
--     rep_email STRING,   -- matches current_user()
--     company   STRING    -- matches the `company` column on every view
--   );
--
--   -- b) row filter: TRUE only for rows whose company the current rep owns.
--   --    Account admins bypass via is_account_group_member.
--   CREATE OR REPLACE FUNCTION :catalog.:schema.rep_can_see(company STRING)
--   RETURNS BOOLEAN
--   RETURN is_account_group_member('mirion_admins')
--       OR EXISTS (SELECT 1 FROM :catalog.:schema.rep_accounts m
--                  WHERE m.rep_email = current_user() AND m.company = company);
--
--   -- c) attach the filter to each backing table (functions/views inherit it):
--   ALTER TABLE :catalog.:schema.src_vw_orders        SET ROW FILTER :catalog.:schema.rep_can_see ON (company);
--   ALTER TABLE :catalog.:schema.src_vw_opportunities SET ROW FILTER :catalog.:schema.rep_can_see ON (company);
--   ALTER TABLE :catalog.:schema.src_vw_winloss       SET ROW FILTER :catalog.:schema.rep_can_see ON (company);
--   ALTER TABLE :catalog.:schema.src_vw_rmas          SET ROW FILTER :catalog.:schema.rep_can_see ON (company);
--   ALTER TABLE :catalog.:schema.src_vw_pprs          SET ROW FILTER :catalog.:schema.rep_can_see ON (company);
--   ALTER TABLE :catalog.:schema.src_vw_field_notes   SET ROW FILTER :catalog.:schema.rep_can_see ON (company);
--   ALTER TABLE :catalog.:schema.src_vw_actions       SET ROW FILTER :catalog.:schema.rep_can_see ON (company);
--
-- After (c), a rep's brief silently contains only their accounts' rows — the
-- agent, functions, and views are untouched.
