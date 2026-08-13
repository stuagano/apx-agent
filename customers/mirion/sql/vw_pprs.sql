-- vw_pprs: SharePoint source (synthetic-backed on fevm-hvhhmh).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.mirion_precall.vw_pprs (
    company, ppr_id, description, severity, status
) AS
SELECT company, ppr_id, description, severity, status
FROM main.mirion_precall.sharepoint_pprs;
