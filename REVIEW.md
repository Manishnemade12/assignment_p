# Starter Code Review

The starter implemented only the happy path. The following defects were found
by comparing its behavior with the API contract and data model.

| # | Location | Defect | How to reproduce or notice it | Resolution |
|---|---|---|---|---|
| 1 | `health` | Always returned 200 without checking MongoDB readiness. | Stop MongoDB and call `GET /health`; it still reports success. | Ping MongoDB and return 503 when it is unavailable. |
| 2 | `compute_late_minutes` | Used a naive replacement of the punch-in date; it did not model IST/overnight shifts or the strict grace boundary. | Test a UTC instant near midnight or exactly 10 minutes after shift start. | Calculate the IST shift start for the record date and apply `> 10` minutes. |
| 3 | `compute_work_hours` | Used ordinary float rounding instead of required decimal half-up rounding. | Use a duration whose third decimal place is exactly 5. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Did not handle overnight shift ends and counted any positive overtime instead of requiring 30 minutes. | Test a night-shift checkout after midnight or a checkout 5 minutes after shift end. | Resolve the correct shift-end instant and apply the 30-minute threshold. |
| 5 | `EmployeeIn` / `create_employee` | Did not validate contract formats, valid dates, or different shift times; duplicate handling used a check-then-insert race. | Send invalid employee data or issue concurrent creates. | Add Pydantic validation, reject equal shifts, and enforce uniqueness with a database index. |
| 6 | `create_employee` and responses | Stored/returned naive Python datetimes instead of the contract's epoch milliseconds. | Inspect `created_at` in the response. | Store UTC datetimes and centralize BSON-datetime to epoch-millisecond serialization. |
| 7 | `list_employees` | Skipped `page * page_size`, counted the unfiltered collection, and did not sort or validate pagination. | Page 1 omits records and department totals are incorrect. | Use `(page - 1) * page_size`, filtered counts, deterministic sort, and bounds validation. |
| 8 | `punch_in` | Did not handle unknown employees, invalid statuses, UTC conversion, truncation, or concurrent duplicate dates correctly. | Use an unknown code, invalid status, or two simultaneous requests. | Validate the employee/status, normalize the instant, compute the R1 date, and rely on the unique natural-key index. |
| 9 | `list_attendance` | Loaded and sorted the whole collection in Python, exposed `_id`, and omitted date/status validation. | Use a large collection or inspect a response. | Apply filters, sort, count, and pagination in MongoDB and serialize only public fields. |
| 10 | Missing routes | Punch-out, regularization, analytics, and explain endpoints were absent. | The contract routes returned 404. | Implement all documented routes, shared rule helpers, audit history, and indexes. |

## Deliberately retained behavior

- The two collection names, `employees` and `attendance_logs`, were retained
  because they are part of the supplied data model.
- `emp_code` remains the public employee identifier; MongoDB `_id` is not used
  in the API.
- The starter's default shift and `PRESENT` status were retained because they
  match the contract, but requests are now validated.
- The starter's use of synchronous PyMongo was retained because the assignment
  requires PyMongo/MongoDB and the endpoints are synchronous functions.
