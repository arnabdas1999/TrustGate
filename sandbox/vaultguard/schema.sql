-- VaultGuard: a payments assistant that schedules vendor wires. Seeded per case by the adapter.

CREATE TABLE vendors (
    name TEXT PRIMARY KEY,
    is_new INTEGER NOT NULL,             -- 1: first payment to this vendor
    country TEXT NOT NULL
);

CREATE TABLE requests (
    id TEXT PRIMARY KEY,
    vendor TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    invoice TEXT NOT NULL,
    requested_by TEXT NOT NULL
);

CREATE TABLE wires (                     -- the ledger
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    memo TEXT NOT NULL,
    status TEXT NOT NULL,                -- scheduled | pending_approval | rejected
    approval_id INTEGER,
    created_by TEXT NOT NULL,            -- seed | agent
    hours_ago REAL NOT NULL DEFAULT 0
);

CREATE TABLE approvals (                 -- dual control (CFO) requests
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL                 -- pending | approved (agents only ever create pending)
);

CREATE TABLE status_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,                -- SCHEDULED | AWAITING_APPROVAL | REJECTED | NEEDS_INFO
    summary TEXT NOT NULL
);
