-- vw_actions: SFDC source (synthetic-backed on fevm-hvhhmh).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.mirion_precall.vw_actions (
    company, action, due_date, status
) AS
SELECT company, action, due_date, status
FROM main.mirion_precall.sfdc_actions;
