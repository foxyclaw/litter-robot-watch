"""Alert delivery must survive a network blip (queue + retry), offline."""
import asyncio
import json

import notifier as notifier_mod
from notifier import TelegramNotifier, backoff_delay


class Flaky(TelegramNotifier):
    """Notifier whose network is scripted: each entry = (ok, retryable, retry_after)."""

    def __init__(self, script, **kw):
        super().__init__("TOKEN", "-100", **kw)
        self.script = list(script)
        self.sent = []

    async def _post(self, message, chat_id=None):
        result = self.script.pop(0) if self.script else (True, False, 0.0)
        if result[0]:
            self.sent.append((str(chat_id or self.chat_id), message))
        return result


DOWN = (False, True, 0.0)
OK = (True, False, 0.0)
BAD_REQUEST = (False, False, 0.0)


def test_backoff_is_exponential_and_capped():
    assert [backoff_delay(n) for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert backoff_delay(50) == notifier_mod.RETRY_MAX_SECONDS


def test_send_reliable_delivers_immediately_when_network_is_up():
    async def scenario():
        n = Flaky([OK])
        assert await n.send_reliable("hello") is True
        assert n.sent == [("-100", "hello")] and n.pending == 0
    asyncio.run(scenario())


def test_alert_is_queued_during_outage_and_delivered_later(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier_mod, "RETRY_BASE_SECONDS", 0.01)
    monkeypatch.setattr(notifier_mod, "backoff_delay", lambda attempt, **kw: 0.01)

    async def scenario():
        outbox = tmp_path / "outbox.json"
        n = Flaky([DOWN, DOWN, DOWN, OK, OK], outbox_path=str(outbox))
        assert await n.send_reliable("🚨 dump alert") is False     # network down -> queued
        assert await n.send_reliable("second alert") is False
        assert n.pending == 2 and n.sent == []
        assert len(json.loads(outbox.read_text())) == 2            # persisted for restarts
        for _ in range(200):                                       # background retry loop
            await asyncio.sleep(0.01)
            if n.pending == 0:
                break
        assert [m for _c, m in n.sent] == ["🚨 dump alert", "second alert"]   # order kept
        assert json.loads(outbox.read_text()) == []
    asyncio.run(scenario())


def test_outbox_is_reloaded_after_restart(tmp_path):
    async def scenario():
        outbox = tmp_path / "outbox.json"
        n1 = Flaky([DOWN], outbox_path=str(outbox))
        n1._retry_loop = lambda: asyncio.sleep(0)                  # "process dies" before retrying
        await n1.send_reliable("lost? no.")
        n2 = Flaky([OK], outbox_path=str(outbox))
        assert n2.pending == 1
        assert await n2.flush() == 0
        assert n2.sent == [("-100", "lost? no.")]
    asyncio.run(scenario())


def test_non_retryable_error_is_dropped_not_queued():
    async def scenario():
        n = Flaky([BAD_REQUEST])
        assert await n.send_reliable("<b>broken html") is False
        assert n.pending == 0
    asyncio.run(scenario())


def test_send_message_quick_retries(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(notifier_mod.asyncio, "sleep", no_sleep)

    async def scenario():
        n = Flaky([DOWN, DOWN, OK])
        assert await n.send_message("stock alert") is True
        n = Flaky([DOWN, DOWN, DOWN])
        assert await n.send_message("stock alert") is False
        assert n.pending == 0                                      # plain send never queues
    asyncio.run(scenario())
