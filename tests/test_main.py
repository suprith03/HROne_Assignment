import unittest
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import ValidationError

from app import main


class AttendanceRuleTests(unittest.TestCase):
    def test_late_grace_is_strict_and_floors_minutes(self):
        exact_grace = datetime(2026, 7, 6, 4, 10, tzinfo=timezone.utc)
        after_grace = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
        later = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)

        self.assertEqual(main.compute_late_minutes(exact_grace, "09:30"), 0)
        self.assertEqual(main.compute_late_minutes(after_grace, "09:30"), 10)
        self.assertEqual(main.compute_late_minutes(later, "09:30"), 45)

    def test_overnight_attendance_date_and_overtime(self):
        after_midnight_ist = datetime(2026, 7, 6, 19, 30, tzinfo=timezone.utc)

        self.assertEqual(main.attendance_date_for_punch(after_midnight_ist, "22:00", "06:00"), "2026-07-06")
        self.assertEqual(main.compute_overtime_minutes(
            datetime(2026, 7, 7, 1, 10, tzinfo=timezone.utc), "22:00", "06:00", "2026-07-06"
        ), 40)

    def test_epoch_instants_are_truncated_to_seconds(self):
        value = main.ms_to_dt(1783312500999)

        self.assertEqual(value.microsecond, 0)
        self.assertEqual(main.dt_to_ms(value), 1783312500000)

    def test_half_up_work_hours_and_half_day_threshold(self):
        punch_in = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)
        rounded_half_up = main.compute_work_hours(punch_in, punch_in.replace(hour=2, minute=20, second=42))

        self.assertEqual(rounded_half_up, 2.35)
        self.assertTrue(main.compute_half_day(4.49))
        self.assertFalse(main.compute_half_day(4.5))

    def test_month_parser_validates_real_calendar_months(self):
        start, end = main.parse_month("2024-02")

        self.assertEqual((start.isoformat(), end.isoformat()), ("2024-02-01", "2024-02-29"))
        with self.assertRaises(HTTPException):
            main.parse_month("2024-13")


class RequestValidationTests(unittest.TestCase):
    def test_epoch_milliseconds_must_be_strict_integers_in_range(self):
        for invalid in (1_783_312_500, 1_783_312_500.0, "1783312500000", True):
            with self.subTest(value=invalid), self.assertRaises(ValidationError):
                main.PunchInRequest(emp_code="EMP0001", punched_at=invalid)

    def test_derived_fields_are_ignored_from_punch_requests(self):
        request = main.PunchInRequest(emp_code="EMP0001", work_hours=100, late_minutes=500)

        self.assertNotIn("work_hours", request.model_dump())
        self.assertNotIn("late_minutes", request.model_dump())

    def test_employee_email_and_real_join_date_are_validated(self):
        employee = {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "not-an-email",
            "department": "Engineering",
            "joined_on": "2026-02-30",
        }

        with self.assertRaises(ValidationError):
            main.EmployeeCreate(**employee)


class ContractAndPipelineTests(unittest.TestCase):
    def test_all_contract_endpoints_are_registered(self):
        paths = main.app.openapi()["paths"]

        for path in (
            "/health",
            "/employees",
            "/attendance/punch-in",
            "/attendance/punch-out",
            "/attendance",
            "/attendance/{emp_code}/{date}",
            "/analytics/employees/{emp_code}/monthly",
            "/analytics/departments/summary",
            "/analytics/leaderboard/late",
            "/analytics/departments/{department}/trend",
            "/admin/explain/{endpoint}",
        ):
            self.assertIn(path, paths)

    def test_leaderboard_ranks_ties_before_applying_rank_limit(self):
        pipeline = main.leaderboard_pipeline(
            datetime(2026, 7, 1).date(), datetime(2026, 7, 31).date(), limit=2
        )
        window = next(stage["$setWindowFields"] for stage in pipeline if "$setWindowFields" in stage)
        rank_filter = next(stage["$match"] for stage in pipeline if stage.get("$match", {}).get("rank"))

        self.assertEqual(window["sortBy"], {"total_late_minutes": -1})
        self.assertEqual(rank_filter, {"rank": {"$lte": 2}})

    def test_trend_pipeline_generates_days_and_uses_rolling_window(self):
        pipeline = main.department_trend_pipeline("Engineering", "2026-07-01", 31)
        date_range = next(stage["$set"]["days"]["$map"]["input"] for stage in pipeline if "$set" in stage and "days" in stage["$set"])
        moving_window = next(stage["$setWindowFields"]["output"]["moving_avg_7d"]["window"] for stage in pipeline if "$setWindowFields" in stage)

        self.assertEqual(date_range, {"$range": [0, 31]})
        self.assertEqual(moving_window, {"documents": [-6, "current"]})


if __name__ == "__main__":
    unittest.main()