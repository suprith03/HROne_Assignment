# Review of the starter implementation

| # | Where | Defect and symptom | Fix |
|---|---|---|---|
| 1 | `ensure_indexes` | It synchronously iterated a Motor index cursor; startup could fail before readiness. | Awaited Mongo index creation directly; named `create_index` calls are idempotent. |
| 2 | `PunchInRequest`, `PunchOutRequest`, `RegularizeRequest` | Pydantic coerced floats and strings into integers before the epoch validator; sub-second instants were stored. | Use strict integer fields, enforce the epoch range, and truncate punch instants before storing/calculating. |
| 3 | `compute_late_minutes` | Flooring the elapsed minutes before comparing to 10 misclassified 09:40:00.900/09:40:01 around the strict grace boundary. | Compare elapsed seconds to 600, then floor the full elapsed minutes. |
| 4 | `/attendance/punch-in` | A check-then-insert race depended on catching arbitrary insert errors and could return the wrong status. | Enforce unique `(emp_code, date)` and translate only `DuplicateKeyError` to 409. |
| 5 | `/attendance/punch-out` | Filtering for an open record could skip the most recent closed record and select an older one; read-then-write allowed two 200 responses. | Select the most recent eligible record, report an already-closed conflict, and conditionally update only while `punch_out` is null. Added the lookup index. |
| 6 | `/attendance/{emp_code}/{date}` | History stored time changes as epoch integers instead of BSON dates; concurrent corrections could overwrite each other's history. A supplied punch-in was not persisted when promoting an absent record. | Store BSON datetimes in change entries, use a guarded atomic update plus `$push`, and set newly supplied punch-ins. |
| 7 | Correction and analytics calculations | Changing a punch-in could leave an existing punch-out more than 24 hours away; analytics counted weekend presence as present days. | Validate the final time pair and apply weekday filtering to present-day aggregation expressions. |
| 8 | Department summary and trend | The handlers loaded employee lists and queried once per department/day; trend output did not reliably satisfy range, rate, or moving-window rules. | Move the cross-collection metrics, calendar generation, and rolling average into MongoDB pipelines. |
| 9 | Late leaderboard | Including employee code in the rank sort broke ties; limiting after Python loading was unbounded. | Use `$rank` on total minutes alone and filter `rank <= limit` in the pipeline, then sort tied rows by code. |
| 10 | `/admin/explain/{endpoint}` | It did not request `executionStats` and several plans did not match the corresponding endpoint query. | Build explain commands from the same query/pipeline builders and request `executionStats`. |
| 11 | Request validation | Month/date parsing could raise uncaught `ValueError`; email checking only looked for `@` and a dot. | Validate canonical calendar values and email format and return 422 for invalid input. |

## Items reviewed and kept

- Employee and attendance list endpoints already use MongoDB filtering, sorting, count, and `(page - 1) * page_size`; they did not need Python-side loading or a pagination rewrite.
- UTC BSON datetimes with epoch-millisecond serialization are the right boundary representation; calendar dates and shift times remain strings in the data model.
- Motor is appropriate for this async FastAPI service, and the single-file application matches the assignment submission format.
