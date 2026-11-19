"""Deterministic Security Mode tests; no camera, server, or network required."""

import unittest
from datetime import datetime, timedelta

from security_events import SecurityEventTracker


class SecurityEventTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = SecurityEventTracker()

    def observe_sequence(self, tracker, times):
        return [tracker.observe(True, now) for now in times]

    def first_event(self, tracker=None):
        tracker = tracker or self.tracker
        tracker.set_enabled(True)
        results = self.observe_sequence(tracker, [0, 0.4, 0.8, 1.2, 1.6, 2.0])
        self.assertEqual(results[:-1], [None] * 5)
        self.assertIsNotNone(results[-1])
        return results[-1]

    def test_disabled_mode_does_not_record_motion(self):
        self.assertFalse(self.tracker.enabled)
        results = self.observe_sequence(self.tracker, [n * 0.4 for n in range(31)])
        self.assertTrue(all(event is None for event in results))
        self.assertEqual(self.tracker.latest_event_id, 0)
        self.assertEqual(self.tracker.events_after(0), [])

    def test_short_motion_does_not_qualify(self):
        self.tracker.set_enabled(True)
        self.assertEqual(
            self.observe_sequence(self.tracker, [0, 0.4, 0.8]),
            [None, None, None],
        )
        self.assertIsNone(self.tracker.observe(False, 1.5))
        self.assertIsNone(self.tracker.observe(False, 4.0))
        self.assertEqual(self.tracker.events_after(0), [])

    def test_sustained_motion_creates_an_unconfirmed_candidate(self):
        event = self.first_event()
        self.assertEqual(event["event_id"], 1)
        self.assertIsInstance(self.tracker.session_id, str)
        self.assertTrue(self.tracker.session_id)
        self.assertEqual(event["session_id"], self.tracker.session_id)
        self.assertEqual(event["type"], "motion_candidate")
        self.assertIs(event["human_confirmed"], False)
        self.assertIs(event["notification_sent"], False)
        timestamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        self.assertIsNotNone(timestamp.tzinfo)
        self.assertEqual(timestamp.utcoffset(), timedelta(0))
        self.assertEqual(self.tracker.latest_event_id, 1)

    def test_continuous_motion_does_not_repeat_after_cooldown(self):
        event = self.first_event()
        results = self.observe_sequence(self.tracker, [n * 0.4 for n in range(6, 51)])
        self.assertTrue(all(candidate is None for candidate in results))
        self.assertEqual(self.tracker.events_after(0), [event])

    def test_short_quiet_gap_does_not_restart_qualification(self):
        self.tracker.set_enabled(True)
        self.assertIsNone(self.tracker.observe(True, 0.0))
        self.assertIsNone(self.tracker.observe(False, 0.2))
        results = self.observe_sequence(self.tracker, [0.4, 0.8, 1.2, 1.6, 2.0])
        self.assertEqual(results[:-1], [None] * 4)
        self.assertIsNotNone(results[-1])

    def test_long_gap_between_motion_samples_restarts_qualification(self):
        self.tracker.set_enabled(True)
        self.observe_sequence(self.tracker, [0.0, 0.4, 0.8])
        results = self.observe_sequence(self.tracker, [1.5, 1.9, 2.3, 2.7, 3.1, 3.5])
        self.assertEqual(results[:-1], [None] * 5)
        self.assertIsNotNone(results[-1])

    def test_new_episode_obeys_cooldown_then_can_record_again(self):
        self.first_event()
        self.assertIsNone(self.tracker.observe(False, 2.7))
        results = self.observe_sequence(self.tracker, [3.0, 3.4, 3.8, 4.2, 4.6, 5.0])
        self.assertEqual(results, [None] * 6)
        self.assertIsNone(self.tracker.observe(False, 6.0))
        results = self.observe_sequence(
            self.tracker, [10.0, 10.4, 10.8, 11.2, 11.6, 12.0]
        )
        self.assertEqual(results[:-1], [None] * 5)
        self.assertEqual(results[-1]["event_id"], 2)
        self.assertEqual(len(self.tracker.events_after(0)), 2)

    def test_disabling_mid_qualification_requires_fresh_motion_after_enable(self):
        self.tracker.set_enabled(True)
        self.observe_sequence(self.tracker, [0.0, 0.4, 0.8, 1.2, 1.6])
        self.tracker.set_enabled(False)
        self.assertIsNone(self.tracker.observe(True, 2.0))
        self.tracker.set_enabled(True)
        results = self.observe_sequence(self.tracker, [3.0, 3.4, 3.8, 4.2, 4.6, 5.0])
        self.assertEqual(results[:-1], [None] * 5)
        self.assertEqual(results[-1]["event_id"], 1)

    def test_repeated_enable_request_does_not_erase_pending_motion(self):
        self.tracker.set_enabled(True)
        self.observe_sequence(self.tracker, [0.0, 0.4, 0.8])
        self.tracker.set_enabled(True)
        results = self.observe_sequence(self.tracker, [1.2, 1.6, 2.0])
        self.assertEqual(results[:-1], [None, None])
        self.assertIsNotNone(results[-1])

    def test_reset_pending_discards_partial_motion_and_preserves_mode(self):
        self.tracker.set_enabled(True)
        self.observe_sequence(self.tracker, [0.0, 0.4, 0.8, 1.2, 1.6])
        self.tracker.reset_pending()
        self.assertTrue(self.tracker.enabled)
        results = self.observe_sequence(self.tracker, [2.0, 2.4, 2.8, 3.2, 3.6, 4.0])
        self.assertEqual(results[:-1], [None] * 5)
        self.assertIsNotNone(results[-1])

    def test_reenable_and_pending_reset_preserve_history_and_cooldown(self):
        event = self.first_event()
        session_id = self.tracker.session_id
        self.tracker.set_enabled(False)
        self.tracker.set_enabled(True)
        self.tracker.reset_pending()
        self.assertEqual(self.tracker.session_id, session_id)
        self.assertEqual(self.tracker.events_after(0), [event])
        results = self.observe_sequence(self.tracker, [3.0, 3.4, 3.8, 4.2, 4.6, 5.0])
        self.assertEqual(results, [None] * 6)
        self.assertEqual(self.tracker.latest_event_id, 1)

    def test_event_reads_are_nonconsuming_defensive_and_bounded(self):
        tracker = SecurityEventTracker(cooldown_seconds=0.0, max_events=2)
        tracker.set_enabled(True)
        for start in [0.0, 3.0, 6.0]:
            if start:
                self.assertIsNone(tracker.observe(False, start - 0.2))
            results = self.observe_sequence(
                tracker, [start, start + 0.4, start + 0.8,
                          start + 1.2, start + 1.6, start + 2.0]
            )
            self.assertIsNotNone(results[-1])
        self.assertEqual(tracker.latest_event_id, 3)
        retained = tracker.events_after(0)
        self.assertEqual([event["event_id"] for event in retained], [2, 3])
        self.assertEqual(tracker.events_after(0), retained)
        self.assertEqual(tracker.events_after(2), [retained[1]])
        self.assertEqual(tracker.events_after(3), [])
        retained[0]["type"] = "tampered"
        retained.pop()
        self.assertEqual(
            [event["type"] for event in tracker.events_after(0)],
            ["motion_candidate", "motion_candidate"],
        )


if __name__ == "__main__":
    unittest.main()
