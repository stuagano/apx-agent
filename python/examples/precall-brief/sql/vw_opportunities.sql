-- vw_opportunities: CRM source (synthetic-backed on sandbox).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.precall.vw_opportunities (
    company, opportunity, stage, value, close_date
) AS
SELECT company, opportunity, stage, value, close_date
FROM main.precall.src_vw_opportunities;
