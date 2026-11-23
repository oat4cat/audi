import unittest

from parser import reconcile_schedules


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


if __name__ == "__main__":
    unittest.main()
