# DECISIONS.md

1. **Indexes.** Five indexes are created at startup (idempotent):
   - `employees.emp_code` unique — enforces uniqueness and makes every employee lookup O(log n).
   - `employees.department` — serves the department filter in `GET /employees` and the `$lookup` in analytics pipelines.
   - `attendance_logs.(emp_code, date)` unique — the natural key; uniqueness is the concurrency guard for punch-in (DuplicateKeyError replaces the check-then-insert race).
   - `attendance_logs.(date DESC, emp_code ASC)` — matches the sort order of `GET /attendance`, turning it from a COLLSCAN into an IXSCAN that also covers skip/limit.
   - `attendance_logs.(date, emp_code, late_minutes)` — supports the leaderboard and monthly analytics aggregations that filter by date range and group by emp_code.
   Considered and rejected: a standalone index on `punch_in` for the punch-out lookup — the `(emp_code, date)` index already narrows the candidate set to at most one record per employee per day, making an additional index redundant.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Both pass the employee lookup (read-only). Both compute the same attendance date `d`. Both call `insert_one` with `{emp_code, date: d}`. The unique compound index on `(emp_code, date)` causes the second insert to throw a `DuplicateKeyError` at the database level — no application-level check is needed. The first insert wins and returns 201; the second is caught and returns 409. Because the uniqueness is enforced inside a single atomic write, no window exists between check and insert.

3. **Ties.** Competition ranking is applied in Python after sorting by `total_late_minutes` descending, `emp_code` ascending. If two employees share the same `total_late_minutes`, they receive the same rank and the next rank is skipped (1, 2, 2, 4). `limit` is applied by filtering rows where `rank <= limit`, not by slicing the array. So if the cutoff rank has two tied employees, both are returned — the response may contain more than `limit` rows.

4. **Headcount.** The department summary starts from the `employees` collection (left side of the join), not from `attendance_logs`. Employees with `joined_on <= last day of the month` are counted into `headcount` regardless of whether they have any logs. The aggregation then does a `$lookup` from logs to employees to pull in department — meaning an employee with zero logs for the month still contributes to headcount but contributes 0 to `present_days`, `late_count`, etc.

5. **One thing I would change at 100× scale.** Add a shard key of `{date: 1, emp_code: 1}` on `attendance_logs` so range queries by date scatter across shards rather than hitting one. Pair it with read-replica routing for all analytics endpoints (they are read-only and latency-tolerant). For the department trend endpoint specifically, pre-aggregate a daily `department_daily_stats` collection updated by a change-stream worker so the trend query becomes a simple range scan instead of a full lookup + densify pipeline.
