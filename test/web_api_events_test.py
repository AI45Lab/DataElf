import unittest


from web_api.events import JobEventBus


class WebApiEventsTest(unittest.TestCase):
    def test_event_bus_assigns_ids_and_replays_events(self):
        bus = JobEventBus()

        first = bus.publish("job_1", {"type": "job.created", "status": "pending"})
        second = bus.publish("job_1", {"type": "job.running", "status": "running"})

        self.assertEqual(first["event_id"], 1)
        self.assertEqual(second["event_id"], 2)
        self.assertEqual(
            [event["type"] for event in bus.replay("job_1")],
            ["job.created", "job.running"],
        )

    def test_event_bus_filters_replay_after_event_id(self):
        bus = JobEventBus()
        bus.publish("job_1", {"type": "job.created"})
        bus.publish("job_1", {"type": "job.running"})
        bus.publish("job_1", {"type": "job.completed"})

        replayed = bus.replay("job_1", after_event_id=1)

        self.assertEqual([event["type"] for event in replayed], ["job.running", "job.completed"])


if __name__ == "__main__":
    unittest.main()
