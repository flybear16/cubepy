-- Sample data for CubePy integration tests. Executed once against a fresh DB.
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
    id        INTEGER PRIMARY KEY,
    country   TEXT,
    tenant_id INTEGER
);

CREATE TABLE orders (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id),
    amount     NUMERIC(12, 2),
    status     TEXT,
    created_at TIMESTAMP,
    tenant_id  INTEGER
);

INSERT INTO users (id, country, tenant_id) VALUES
    (1, 'CN', 42),
    (2, 'JP', 42),
    (3, 'US', 99);

INSERT INTO orders (id, user_id, amount, status, created_at, tenant_id) VALUES
    (1, 1, 10.00, 'shipped',  '2026-08-01 10:00:00', 42),
    (2, 2, 30.00, 'shipped',  '2026-08-02 10:00:00', 42),
    (3, 1,  5.00, 'pending',  '2026-08-03 10:00:00', 42),
    (4, 3, 100.00, 'shipped', '2026-08-04 10:00:00', 99);
