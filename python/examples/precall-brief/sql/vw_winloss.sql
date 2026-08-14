-- vw_winloss: CRM source (synthetic-backed on sandbox).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.precall.vw_winloss (
    company, outcome, product, date
) AS
SELECT company, outcome, product, date
FROM main.precall.src_vw_winloss;
