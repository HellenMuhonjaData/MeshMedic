# Quality Contract - skill-lab/orders.csv

## Rules

| Field | Rule |
|---|---|
| `order_id` | Must be unique across all rows (primary key). |
| `region` | Required; must not be null or blank. |
| `revenue` | Must be greater than zero. |
| `load_timestamp` | Must be less than 24 hours old at time of check. |
| Row count | Expected row count is at least 10. |