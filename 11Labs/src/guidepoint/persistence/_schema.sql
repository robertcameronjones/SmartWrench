CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY NOT NULL,
    data TEXT NOT NULL,
    state TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    vehicle_vin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_terminal INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cases_state ON cases(state);
CREATE INDEX IF NOT EXISTS idx_cases_customer_phone ON cases(customer_phone);
CREATE INDEX IF NOT EXISTS idx_cases_vehicle_vin ON cases(vehicle_vin);
CREATE INDEX IF NOT EXISTS idx_cases_created_at ON cases(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cases_is_terminal ON cases(is_terminal);
