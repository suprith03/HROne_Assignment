Employee indexes: unique `emp_code`, `(department, joined_on, emp_code)`, and `joined_on`. Attendance indexes: unique `(emp_code, date)`, `(emp_code, date DESC)`, `(emp_code, punch_in DESC)`, `(date DESC, emp_code)`, and `(status, date)`. These enforce natural-key uniqueness and support department headcount, latest-punch lookup, ordered lists, and date scans.

Concurrent punch-ins may both pass the initial lookup, but the unique compound index allows one insert: the winner gets 201 and the duplicate-key loser gets 409. Punch-out conditionally updates a still-open record, so only one request succeeds. Corrections compare the snapshot before updating and appending history; stale updates get 409.

The leaderboard groups by employee, ranks by total late minutes alone with MongoDB `$rank`, then orders equal ranks by `emp_code`. The rank cutoff is applied after ranking, so a tie at the limit returns every tied employee.

Department summaries start from eligible employees, not logs. The cross-collection lookup therefore retains people with no records, counts those joined by month-end, and omits future joiners.

At 100 times the data volume, I would add asynchronously refreshed daily/monthly summary collections with an explicit freshness target and reconciliation job. Raw logs would remain the source of truth.
