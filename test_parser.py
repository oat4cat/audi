import unittest
from unittest.mock import Mock, patch

import httpx

from parser import (
    ConsecutiveFailureTracker,
    ScheduleFetchError,
    ScheduleSiteUnavailableError,
    collect_group_schedules,
    fetch_group_schedule_with_retry,
    parse_group_html,
    reconcile_schedules,
)


NOW = "2026-11-23T10:00:00"


def slot(auditory, group, **overrides):
    data = {
        "auditory": auditory,
        "date": "23.11.2026",
        "num_para": "1",
        "time": "08:20-09:55",
        "discipline": "Тестовая дисциплина",
        "kind": "Лекция",
        "lecturer": "Тест****в И.И.",
        "group": group,
    }
    data.update(overrides)
    return data


class ReconcileSchedulesTests(unittest.TestCase):
    def test_clears_old_room_when_group_has_no_relevant_classes(self):
        old_data = {
            "100 (10 корпус)": {
                "updated_at": "2026-11-22T10:00:00",
                "schedule": {
                    "23.11.2026": {
                        "1": {
                            "group": "Группа 111111",
                            "source_group_id": "123",
                        }
                    }
                },
            }
        }

        result = reconcile_schedules(
            old_data,
            {"123": ([], {"Группа 111111"})},
            set(),
            NOW,
        )

        self.assertEqual(result, {})

    def test_moves_group_from_old_room_after_successful_refresh(self):
        old_data = {
            "100 (10 корпус)": {
                "updated_at": "2026-11-22T10:00:00",
                "schedule": {
                    "23.11.2026": {
                        "1": {
                            "time": "08:20-09:55",
                            "status": "busy",
                            "group": "Группа 111111",
                            "source_group_id": "123",
                            "updated_at": "2026-11-22T10:00:00",
                        }
                    }
                },
            }
        }

        result = reconcile_schedules(
            old_data,
            {
                "123": (
                    [slot("200 (10 корпус)", "Группа 111111")],
                    {"Группа 111111"},
                )
            },
            set(),
            NOW,
        )

        self.assertNotIn("100 (10 корпус)", result)
        self.assertIn("1", result["200 (10 корпус)"]["schedule"]["23.11.2026"])

    def test_preserves_failed_group_and_replaces_successful_legacy_group(self):
        old_data = {
            "100 (10 корпус)": {
                "updated_at": "2026-11-22T10:00:00",
                "schedule": {
                    "23.11.2026": {
                        "1": {
                            "group": "Группа 111111",
                            "source_group_id": "failed",
                        },
                        "2": {
                            "group": "Группа 222222",
                        },
                    }
                },
            }
        }

        result = reconcile_schedules(
            old_data,
            {
                "success": (
                    [slot("200 (11 корпус)", "Группа 222222", num_para="2")],
                    {"Группа 222222"},
                )
            },
            {"failed"},
            NOW,
        )

        old_slots = result["100 (10 корпус)"]["schedule"]["23.11.2026"]
        self.assertIn("1", old_slots)
        self.assertNotIn("2", old_slots)
        self.assertIn("2", result["200 (11 корпус)"]["schedule"]["23.11.2026"])


class FetchGroupScheduleTests(unittest.TestCase):
    @staticmethod
    def schedule_html(group="Группа 111111"):
        return f"""
            <a class="navbar-brand">{group}</a>
            <div class="list col-md-2">
                <div class="dayofweek">Понедельник, 23.11.2026</div>
            </div>
        """

    def test_retries_transient_network_error_once(self):
        client = Mock()
        client.get.side_effect = [
            httpx.ReadTimeout("timeout"),
            Mock(status_code=200, text=self.schedule_html()),
        ]

        with patch("parser.time.sleep") as sleep:
            result = fetch_group_schedule_with_retry(client, "123")

        self.assertIn("dayofweek", result)
        self.assertEqual(client.get.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_retries_invalid_success_response_once(self):
        client = Mock()
        client.get.side_effect = [
            Mock(status_code=200, text="<html>Service temporarily unavailable</html>"),
            Mock(status_code=200, text=self.schedule_html()),
        ]

        with patch("parser.time.sleep"):
            result = fetch_group_schedule_with_retry(client, "123")

        self.assertIn("navbar-brand", result)
        self.assertEqual(client.get.call_count, 2)

    def test_raises_after_one_retry_for_persistent_failure(self):
        client = Mock()
        client.get.return_value = Mock(status_code=429, text="")

        with patch("parser.time.sleep"), self.assertRaises(ScheduleFetchError):
            fetch_group_schedule_with_retry(client, "123")

        self.assertEqual(client.get.call_count, 2)

    def test_server_error_aborts_without_retry(self):
        client = Mock()
        client.get.return_value = Mock(status_code=503, text="")

        with self.assertRaises(ScheduleSiteUnavailableError):
            fetch_group_schedule_with_retry(client, "123")

        client.get.assert_called_once()

    def test_accepts_schedule_layout_without_col_md_2(self):
        client = Mock()
        html = """
            <a class="navbar-brand">Группа 113501</a>
            <div class="group_list_1 col-md-7">
                <div class="list col-md-3">
                    <div class="dayofweek">Понедельник, 23.11.2026</div>
                </div>
            </div>
        """
        client.get.return_value = Mock(status_code=200, text=html)

        result = fetch_group_schedule_with_retry(client, "20297")

        self.assertEqual(result, html)


class ConsecutiveFailureTests(unittest.TestCase):
    def test_success_resets_failure_count(self):
        failures = ConsecutiveFailureTracker(3)

        self.assertFalse(failures.record_failure())
        self.assertFalse(failures.record_failure())
        failures.record_success()

        self.assertFalse(failures.record_failure())
        self.assertFalse(failures.record_failure())
        self.assertTrue(failures.record_failure())

    def test_collection_stops_at_consecutive_failure_threshold(self):
        client = Mock()

        with patch(
            "parser.fetch_group_schedule_with_retry",
            side_effect=ScheduleFetchError("unavailable"),
        ) as fetch, patch("parser.time.sleep"):
            with self.assertRaisesRegex(
                ScheduleSiteUnavailableError,
                "3 ошибок получения страниц подряд",
            ):
                collect_group_schedules(client, ["1", "2", "3", "4"])

        self.assertEqual(fetch.call_count, 3)


class ParseGroupHtmlTests(unittest.TestCase):
    def test_parses_days_from_alternative_grid_class(self):
        html = """
            <a class="navbar-brand">Группа 113501</a>
            <div class="group_list_1 col-md-7">
                <div class="list col-md-3">
                    <div class="dayofweek">Понедельник, 23.11.2026</div>
                    <div class="timetable_sheet hidden-xs">
                        <div class="num_para">1</div>
                        <div class="time_para">08:20-09:55</div>
                        <div class="auditorium">ауд. 12, набережная Северной Двины, д. 2</div>
                        <div class="discipline">Математика</div>
                        <div class="group">Группа 113501</div>
                    </div>
                </div>
            </div>
        """

        result = parse_group_html(html, "20297")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["auditory"], "12 (11 корпус)")


if __name__ == "__main__":
    unittest.main()
