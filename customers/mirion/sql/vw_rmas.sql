-- vw_rmas: ServiceMax source (synthetic-backed on fevm-hvhhmh).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.mirion_precall.vw_rmas (
    company, rma_id, description, status, date
) AS
SELECT company, rma_id, description, status, date
FROM main.mirion_precall.svcmax_rmas;
