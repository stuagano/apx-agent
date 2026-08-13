-- vw_winloss: SFDC source (synthetic-backed on fevm-hvhhmh).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.mirion_precall.vw_winloss (
    company, outcome, product, date
) AS
SELECT company, outcome, product, date
FROM main.mirion_precall.sfdc_winloss;
