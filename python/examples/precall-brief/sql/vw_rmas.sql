-- vw_rmas: Source system (synthetic-backed on sandbox).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.precall.vw_rmas (
    company, rma_id, description, status, date
) AS
SELECT company, rma_id, description, status, date 
FROM main.precall.src_vw_rmas;
