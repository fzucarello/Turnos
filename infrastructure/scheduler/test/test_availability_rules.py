import unittest
from datetime import date
from infrastructure.scheduler.availability_rules import weekly_rules, can_attend, minute, minimum_date

class AvailabilityRulesTests(unittest.TestCase):
    def setUp(self):
        self.week = '[["09:00","20:00"],["09:00","20:00"],null,["09:00","20:00"],["09:00","20:00"],null,null]'
        self.blocks = '[[["13:00","15:00"],["17:00","18:00"]],[],[],[],[],[],[]]'

    def test_two_exclusions_leave_remainder_available(self):
        rules = weekly_rules(self.week, self.blocks, "09:00", "20:00")
        for hour in ("09:00", "12:59", "15:00", "16:30", "18:00", "20:00"):
            self.assertTrue(can_attend(0, minute(hour), rules), hour)
        for hour in ("08:59", "13:00", "14:59", "17:00", "17:59", "20:01"):
            self.assertFalse(can_attend(0, minute(hour), rules), hour)

    def test_closed_days_are_unavailable_even_when_time_exists(self):
        rules = weekly_rules(self.week, self.blocks, "09:00", "20:00")
        self.assertFalse(can_attend(2, minute("11:30"), rules))
        self.assertTrue(can_attend(3, minute("11:30"), rules))

    def test_reject_overlapping_exclusions_and_outside_day(self):
        cases = [
            '[[["13:00","15:00"],["14:00","16:00"]],[],[],[],[],[],[]]',
            '[[["08:00","09:30"]],[],[],[],[],[],[]]',
            '[[["14:00","14:00"]],[],[],[],[],[],[]]',
            '[[],[],[["10:00","12:00"]],[],[],[],[]]'
        ]
        for blocks in cases:
            with self.subTest(blocks=blocks), self.assertRaises(ValueError):
                weekly_rules(self.week, blocks, "09:00", "20:00")

    def test_minimum_date_is_strict(self):
        self.assertEqual(minimum_date("2026-10-26"), date(2026,10,26))
        self.assertIsNone(minimum_date(""))
        for value in ("26/10/2026", "2026-02-31", "2026-13-02"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                minimum_date(value)

    def test_legacy_hours_are_supported(self):
        rules = weekly_rules("", "", "06:00", "22:00")
        self.assertTrue(can_attend(3,minute("21:30"),rules))
        self.assertFalse(can_attend(3,minute("23:00"),rules))

if __name__ == "__main__":
    unittest.main()
