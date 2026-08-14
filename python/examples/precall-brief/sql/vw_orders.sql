-- vw_orders: ERP source (synthetic-backed on sandbox).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.precall.vw_orders (
    company, order_id, description, qty, expected_ship, status
) AS
SELECT company, order_id, description, qty, expected_ship, status
FROM main.precall.src_vw_orders;
