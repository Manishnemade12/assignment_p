# Design Decisions

1. **Indexes.** `employees.emp_code` is unique because it is the immutable
business identifier. `employees(department, joined_on)` supports department
headcount selection. The unique `attendance_logs(emp_code, date)` index
represents the natural key and prevents duplicate punch-ins. The
`attendance_logs(date, emp_code)` index supports the required attendance sort
and date ranges. The `(emp_code, date, status)` index supports common filtered
attendance reads. A status-first index was considered but rejected because it
would add write overhead without supporting the required sort.

2. **Punch-in race.** Requests may both read the employee, but both then insert
the same natural key. MongoDB's unique index allows exactly one insert; the
loser receives `DuplicateKeyError` and the API returns 409. There is no
application-level check-then-insert decision.

3. **Punch-out and corrections.** Punch-out updates with a predicate requiring
`punch_out` to still be null. Therefore simultaneous requests cannot both
close the record. Corrections use the same optimistic predicate, including the
current history array, so a concurrent edit returns 409 instead of silently
losing an audit entry.

4. **Ties and headcount.** Leaderboard rows are sorted by total late minutes
and employee code, then ranked by first sorted position; ties share a rank and
all rows at the cutoff rank are returned. Department summaries begin with
employees who joined by period end, then overlay logs, so employees with no
logs still count in headcount.

5. **Scale consideration.** At 100x data volume, I would materialize daily and
monthly summaries using a change stream or scheduled job while retaining raw
logs as the audit source. This would keep interactive analytics from scanning
the same raw records repeatedly.
