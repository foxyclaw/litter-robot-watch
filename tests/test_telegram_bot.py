"""Command bot: who gets answered, where replies go, timezone labels, backoff."""
import asyncio

from telegram_bot import TelegramBot, poll_backoff

GROUP = "-1001234"
OWNER = 111


class FakeDB:
    async def get_distinct_robot_ids(self):
        return ["robot-1"]

    async def get_drawer_full_history(self, robot_id, limit=20):
        # newest first, UTC — one in PDT (summer), one in PST (winter)
        return ["2026-11-11 02:10:09", "2026-07-15 19:00:00"]

    async def get_activities(self, robot_id=None, limit=30):
        return [{"robot_id": "robot-1", "timestamp": "2026-11-11 02:10:09",
                 "action": "DFILevelPercent", "action_value": "73"}]


class FakeMonitor:
    robots = {}

    def __init__(self):
        self.acked = 0

    async def acknowledge_incidents(self):
        self.acked += 1
        return ["litter_dump"]


class Bot(TelegramBot):
    def __init__(self, authorized=None):
        super().__init__("TOKEN", GROUP, FakeMonitor(), FakeDB(), authorized_user_ids=authorized)
        self.out = []

    async def _post_with_retry(self, method, make_kwargs, what):
        kwargs = make_kwargs()
        self.out.append((kwargs["json"]["chat_id"], kwargs["json"]["text"]))
        return True


def update(uid, chat_id, chat_type, user_id, text):
    return {"update_id": uid, "message": {
        "chat": {"id": chat_id, "type": chat_type}, "from": {"id": user_id}, "text": text}}


def test_group_command_is_answered_in_group():
    async def scenario():
        bot = Bot()
        await bot._handle_update(update(1, int(GROUP), "supergroup", 999, "/shit"))
        assert len(bot.out) == 1 and bot.out[0][0] == GROUP
    asyncio.run(scenario())


def test_dm_ignored_by_default_group_only():
    async def scenario():
        bot = Bot()
        await bot._handle_update(update(1, OWNER, "private", OWNER, "/shit"))
        assert bot.out == []
    asyncio.run(scenario())


def test_dm_from_authorized_user_is_answered_in_that_dm():
    async def scenario():
        bot = Bot(authorized=[OWNER])
        await bot._handle_update(update(1, OWNER, "private", OWNER, "/shit@SomeBot"))
        assert len(bot.out) == 1 and bot.out[0][0] == str(OWNER)      # not the group
        # an unknown user's DM, and an authorized user in some *other group*, stay ignored
        await bot._handle_update(update(2, 222, "private", 222, "/shit"))
        await bot._handle_update(update(3, -555, "group", OWNER, "/shit"))
        assert len(bot.out) == 1
        # alerts / messages outside a command still default to the group
        await bot.send_message("alert")
        assert bot.out[-1] == (GROUP, "alert")
    asyncio.run(scenario())


def test_times_are_labelled_pst_or_pdt_correctly():
    async def scenario():
        bot = Bot()
        await bot._handle_update(update(1, int(GROUP), "supergroup", 999, "/shit"))
        text = bot.out[0][1]
        assert "Jul 15 12:00 PDT" in text          # 19:00 UTC in summer = UTC-7
        assert "Nov 10 18:10 PST" in text          # 02:10 UTC in winter = UTC-8 (was shown as 19:10 "PDT")
        await bot._handle_update(update(2, int(GROUP), "supergroup", 999, "/activity"))
        assert "Nov 10 18:10:09 PST  DFILevelPercent = 73" in bot.out[1][1]
    asyncio.run(scenario())


def test_ack_command_and_non_commands():
    async def scenario():
        bot = Bot()
        await bot._handle_update(update(1, int(GROUP), "supergroup", 999, "just chatting"))
        assert bot.out == []
        await bot._handle_update(update(2, int(GROUP), "supergroup", 999, "/ack"))
        assert bot.monitor.acked == 1 and "litter_dump" in bot.out[0][1]
    asyncio.run(scenario())


def test_poll_backoff_exponential_with_cap():
    assert [poll_backoff(n) for n in range(1, 8)] == [2, 4, 8, 16, 32, 64, 120]
    assert poll_backoff(1000) == 120


def test_polling_outage_logs_one_warning_not_an_error_per_attempt(caplog, monkeypatch):
    import logging
    import telegram_bot

    async def scenario():
        bot = Bot()
        calls = {"n": 0}

        async def flaky_poll():
            calls["n"] += 1
            if calls["n"] <= 20:
                raise ConnectionResetError("Connection reset by peer")
            bot.stop()

        async def no_sleep(_):
            return None

        bot._poll_updates = flaky_poll
        monkeypatch.setattr(telegram_bot.asyncio, "sleep", no_sleep)
        with caplog.at_level(logging.INFO):
            await bot.start()
    asyncio.run(scenario())
    records = [r for r in caplog.records if "polling" in r.getMessage().lower()]
    assert [r.levelname for r in records] == ["WARNING", "INFO"]       # start of outage + recovery
    assert "recovered after 20 failed attempt(s)" in records[1].getMessage()
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
