-- vw_orders: SAP source (synthetic-backed on fevm-hvhhmh).
-- Frozen column contract (AC-1). Swap->real: repoint the FROM only.
CREATE OR REPLACE VIEW main.mirion_precall.vw_orders (
    company, order_id, description, qty, expected_ship, status
) AS
SELECT company, order_id, description, qty, expected_ship, status
FROM main.mirion_precall.sap_orders;
