-- vw_opportunities: SFDC source (synthetic-backed on fevm-hvhhmh).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.mirion_precall.vw_opportunities (
    company, opportunity, stage, value, close_date
) AS
SELECT company, opportunity, stage, value, close_date
FROM main.mirion_precall.sfdc_opportunities;
