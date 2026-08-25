-- M3 pilot trade domain — Hologres-constrained shape (no PK / no FK).
-- DDL only; deterministic data is generated at runtime by the pilot script
-- (.omc/scripts/m3_pilot.py) via generate_series so reference answers can be
-- cross-checked by direct SQL in the same database.
DROP TABLE IF EXISTS dwd_orders;
DROP TABLE IF EXISTS dim_customer;
DROP TABLE IF EXISTS dim_product;

CREATE TABLE dim_customer (
    id    INTEGER,
    level TEXT
);

CREATE TABLE dim_product (
    id    INTEGER,
    brand TEXT
);

CREATE TABLE dwd_orders (
    id            INTEGER,
    customer_id   INTEGER,
    product_id    INTEGER,
    gmv           NUMERIC(12, 2),
    pay_amount    NUMERIC(12, 2),
    refund_amount NUMERIC(12, 2),
    status        TEXT,
    region        TEXT,
    channel       TEXT,
    category      TEXT,
    is_new        INTEGER,
    created_at    TIMESTAMP,
    tenant_id     INTEGER
);
