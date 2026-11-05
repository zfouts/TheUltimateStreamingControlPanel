import datetime
import unittest

from tests import helpers  # noqa: F401

from plugins.bambu import wording
from plugins.bambu.wording import (clean_job, clean_text, finish_clock, overlay_text, print_values,
                                   status_line, time_left)

CHICAGO = datetime.timezone(datetime.timedelta(hours=-5))
NOW = datetime.datetime(2026, 10, 27, 22, 33, tzinfo=CHICAGO)

RUNNING = {"connected": True, "state": "RUNNING", "active": True, "percent": 35, "remaining_min": 72,
           "job": "skonebone_sign", "layer": 7, "total_layers": 20, "nozzle_temp": 219.6,
           "bed_temp": 55.0, "light": "on", "error": None}
TEMPS = "\n\nNozzle 220°C\nBed 55°C"


class CleanTests(unittest.TestCase):
    def test_clean_text(self):
        self.assertEqual(clean_text("a\r\nb\tc \x1b[31mred\x1b[0m", 50), "abc red")
        self.assertEqual(clean_text("  a   b  ", 50), "a b")
        self.assertEqual(clean_text("x\u202ey\u200bz", 50), "xyz")
        self.assertEqual(clean_text("\U0001F468\u200d\U0001F469", 50), "\U0001F468\u200d\U0001F469")
        self.assertEqual(clean_text("abcdefghij", 5), "abcd…")
        self.assertEqual(clean_text(None, 5), "")
        self.assertEqual(clean_text(12345, 10), "12345")

    def test_clean_job(self):
        self.assertEqual(clean_job("/data/Metadata/plate_2.gcode"), "plate_2")
        self.assertEqual(clean_job("C:\\prints\\Benchy.GCODE.3MF"), "Benchy")
        self.assertEqual(clean_job("thing.3mf"), "thing")
        self.assertEqual(clean_job("a\nb\u202ec"), "abc")
        self.assertIsNone(clean_job(""))
        self.assertIsNone(clean_job(".gcode"))
        self.assertIsNone(clean_job(None))
        long = clean_job("x" * 200)
        self.assertLessEqual(len(long), 64)
        self.assertTrue(long.endswith("…"))


class TimeTests(unittest.TestCase):
    def test_time_left(self):
        for minutes, want in [(None, None), (0, "almost done"), (45, "45 min left"),
                              (89, "1 h 29 min left"), (120, "2 h 0 min left")]:
            self.assertEqual(time_left(minutes), want)

    def test_finish_clock(self):
        self.assertEqual(finish_clock(72, NOW), "11:45 PM")
        self.assertEqual(finish_clock(180, NOW), "Wed 1:33 AM")
        self.assertIsNone(finish_clock(0, NOW))
        self.assertIsNone(finish_clock(None, NOW))


class OverlayTests(unittest.TestCase):
    def test_overlay_text(self):
        cases = [
            ({}, "skonebone_sign\nLayer 7/20\n35%\n1 h 12 min left\ndone ~11:45 PM" + TEMPS),
            ({"remaining_min": 0}, "skonebone_sign\nLayer 7/20\n35%\nalmost done" + TEMPS),
            ({"remaining_min": 180}, "skonebone_sign\nLayer 7/20\n35%\n3 h 0 min left\ndone ~Wed 1:33 AM" + TEMPS),
            ({"layer": None, "job": None}, "35%\n1 h 12 min left\ndone ~11:45 PM" + TEMPS),
            ({"job": "base_shell_2in_plate_2_extra_long"},
             "base_shell_2in_plate_2_…\nLayer 7/20\n35%\n1 h 12 min left\ndone ~11:45 PM" + TEMPS),
            ({"state": "PAUSE"}, "Paused\nskonebone_sign\nLayer 7/20\n35%" + TEMPS),
            ({"state": "PREPARE"}, "Getting ready\nskonebone_sign" + TEMPS),
            ({"state": "FINISH"}, "Print finished\nskonebone_sign" + TEMPS),
            ({"state": "FAILED"}, "Print failed\nor canceled\nskonebone_sign" + TEMPS),
            ({"state": "IDLE"}, "Bob is idle" + TEMPS),
            ({"state": "IDLE", "bed_temp": None}, "Bob is idle\n\nNozzle 220°C"),
            ({"state": None, "nozzle_temp": None, "bed_temp": None}, "Bob is idle"),
            ({"connected": False}, "Printer offline"),
        ]
        for changes, want in cases:
            with self.subTest(changes=changes):
                self.assertEqual(overlay_text({**RUNNING, **changes}, NOW, "Bob"), want)

    def test_only_the_chosen_lines_show(self):
        self.assertEqual(overlay_text(RUNNING, NOW, "Bob", lines=("percent",)), "35%")
        self.assertEqual(overlay_text({**RUNNING, "state": "PAUSE"}, NOW, "Bob", lines=("job",)),
                         "Paused\nskonebone_sign")
        self.assertEqual(overlay_text(RUNNING, NOW, "Bob", lines=("temps",)), "Nozzle 220°C\nBed 55°C")
        self.assertEqual(overlay_text(RUNNING, NOW, "Bob", lines=()), "")

    def test_default_lines(self):
        self.assertIn("temps", wording.STATUS_LINES)


class StatusLineTests(unittest.TestCase):
    def test_status_line(self):
        cases = [
            ({}, "Bob · Printing · 35% · 1 h 12 min left · layer 7/20 · skonebone_sign"),
            ({"state": "PAUSE"}, "Bob · Paused · 35% · layer 7/20 · skonebone_sign"),
            ({"state": "PREPARE"}, "Bob · Getting ready to print · skonebone_sign"),
            ({"state": "FINISH"}, "Bob · Print finished · skonebone_sign"),
            ({"state": "FAILED"}, "Bob · Print failed or was canceled · skonebone_sign"),
            ({"state": "IDLE"}, "Bob · Idle"),
            ({"state": "OTHER_THING", "job": None}, "Bob · Other_Thing"),
            ({"state": None, "job": None}, "Bob · Status unknown"),
            ({"connected": False}, "Bob · Printer offline"),
        ]
        for changes, want in cases:
            with self.subTest(changes=changes):
                self.assertEqual(status_line({**RUNNING, **changes}, "Bob"), want)

    def test_ended_idle_means_stopped(self):
        self.assertEqual(status_line({**RUNNING, "state": "IDLE", "job": None}, "Bob", ended=True),
                         "Bob · Print stopped")

    def test_one_line(self):
        self.assertNotIn("\n", status_line(RUNNING, "Bo\nb"))


class PrintValuesTests(unittest.TestCase):
    def test_running(self):
        v = print_values({**RUNNING, "remaining_min": 62, "percent": 58, "layer": 701, "total_layers": 1058}, "Bob")
        self.assertEqual(set(v), {"printer", "status", "percent", "layer", "time_left", "finish", "job"})
        self.assertEqual(v["status"], "Printing 58% · 1 h 2 min left")
        self.assertEqual(v["percent"], "58%")
        self.assertEqual(v["layer"], "701/1058")
        self.assertEqual(v["time_left"], "1 h 2 min left")
        self.assertTrue(v["finish"].startswith("done ~"))
        self.assertEqual(v["job"], "skonebone_sign")
        self.assertEqual(v["printer"], "Bob")

    def test_other_states(self):
        self.assertEqual(print_values({**RUNNING, "state": "PAUSE", "percent": 58}, "Bob")["status"], "Paused at 58%")
        idle = print_values({**RUNNING, "state": "IDLE", "percent": None, "layer": None, "job": None,
                             "remaining_min": None}, "Bob")
        self.assertEqual(idle["status"], "Idle")
        self.assertEqual((idle["percent"], idle["layer"], idle["time_left"], idle["finish"], idle["job"]),
                         ("", "", "", "", ""))
        self.assertEqual(print_values({**RUNNING, "connected": False}, "Bob")["status"], "Offline")


if __name__ == "__main__":
    unittest.main()
