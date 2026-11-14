"""New group messages wake analysis; the model decides whether to speak."""


class PassiveParticipation:
    def should_evaluate(self, group_id, text, *, enabled) -> bool:
        """A declined reply never puts the next message's analysis to sleep."""
        return bool(enabled and str(group_id or "") and str(text or "").strip())
