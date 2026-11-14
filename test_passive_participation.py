"""Passive evaluation stays awake until explicitly disabled."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import unittest

from passive_participation import PassiveParticipation


class PassiveParticipationTests(unittest.TestCase):
    def setUp(self):
        self.participation = PassiveParticipation()

    def test_every_message_wakes_including_short_continuations(self):
        for text in ("我在思考", "让我想想", "1", "？", "今天聊点什么", "[图片]"):
            self.assertTrue(self.participation.should_evaluate("123", text, enabled=True))

    def test_disabled_autonomy_and_empty_events_are_not_evaluated(self):
        self.assertFalse(self.participation.should_evaluate("123", "你好", enabled=False))
        self.assertFalse(self.participation.should_evaluate("", "你好", enabled=True))
        self.assertFalse(self.participation.should_evaluate("123", "  ", enabled=True))

    def test_evaluation_is_not_consumed_between_turns_or_groups(self):
        for _ in range(3):
            self.assertTrue(self.participation.should_evaluate("123", "你好", enabled=True))
            self.assertTrue(self.participation.should_evaluate("456", "然后呢？", enabled=True))


if __name__ == "__main__":
    unittest.main()
