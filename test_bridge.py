import unittest

from avito_telegram_bridge import Config, EventDeduplicator, render_event


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(
            telegram_bot_token="token",
            telegram_chat_id="@channel",
            webhook_secret="x" * 32,
            profile_names={"100": "Основной & магазин"},
        )

    def event(self, *, author_id="200", text="Здравствуйте <тест>"):
        return {
            "id": "event-1",
            "payload": {
                "type": "message",
                "value": {
                    "id": "message-1",
                    "chat_id": "chat-1",
                    "user_id": 100,
                    "author_id": int(author_id),
                    "type": "text",
                    "content": {"text": text},
                },
            },
        }

    def test_incoming_message_is_rendered_and_escaped(self):
        rendered = render_event(self.event(), self.config)
        self.assertIn("Основной &amp; магазин", rendered)
        self.assertIn("Здравствуйте &lt;тест&gt;", rendered)

    def test_outgoing_message_is_ignored(self):
        self.assertIsNone(render_event(self.event(author_id="100"), self.config))

    def test_duplicate_event_is_detected(self):
        dedupe = EventDeduplicator()
        self.assertFalse(dedupe.is_duplicate("event-1"))
        self.assertTrue(dedupe.is_duplicate("event-1"))

    def test_event_can_be_retried_after_queue_failure(self):
        dedupe = EventDeduplicator()
        self.assertFalse(dedupe.is_duplicate("event-1"))
        dedupe.forget("event-1")
        self.assertFalse(dedupe.is_duplicate("event-1"))


if __name__ == "__main__":
    unittest.main()
