-- vw_actions: Source system (synthetic-backed on sandbox).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.precall.vw_actions (
    company, action, due_date, status
) AS
SELECT company, action, due_date, status 
FROM main.precall.src_vw_actions;
