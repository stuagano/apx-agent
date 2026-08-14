-- vw_pprs: Source system (synthetic-backed on sandbox).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.precall.vw_pprs (
    company, ppr_id, description, severity, status
) AS
SELECT company, ppr_id, description, severity, status 
FROM main.precall.src_vw_pprs;
