-- vw_field_notes: SFDC source (synthetic-backed on fevm-hvhhmh).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.mirion_precall.vw_field_notes (
    company, note, author, date
) AS
SELECT company, note, author, date
FROM main.mirion_precall.sfdc_field_notes;
