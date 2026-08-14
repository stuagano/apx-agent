-- vw_field_notes: Source system (synthetic-backed on sandbox).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.precall.vw_field_notes (
    company, note, author, date
) AS
SELECT company, note, author, date 
FROM main.precall.src_vw_field_notes;
