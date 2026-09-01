"""image_source 多级取图逻辑单元测试（无需 AstrBot 环境）。

运行：
    python -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import unittest

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PLUGIN_DIR)

import image_source


class FakeImage:
    def __init__(self, url=""):
        self.type = "Image"
        self.url = url
        self.file = ""
        self.path = ""


class FakeReply:
    def __init__(self, reply_id="", chain=None):
        self.type = "Reply"
        self.id = reply_id
        self.chain = chain or []


class FakeEvent:
    def __init__(self, components=None, origin="session:group:123", bot=None):
        self._components = components or []
        self.unified_msg_origin = origin
        self.bot = bot

    def get_messages(self):
        return list(self._components)


class FakeBot:
    """模拟 aiocqhttp CQHttp：call_action('get_msg', message_id=...) 返回 OneBot 消息。"""

    def __init__(self, onebot_message=None):
        self.onebot_message = onebot_message or []

    async def call_action(self, action, **kwargs):
        assert action == "get_msg"
        return {"message_id": kwargs["message_id"], "message": self.onebot_message}


def run(coro):
    return asyncio.run(coro)


class TestRecentImageCache(unittest.TestCase):
    def test_record_and_get(self):
        cache = image_source.RecentImageCache()
        cache.record("g1", "https://a/1.png")
        self.assertEqual(cache.get_recent("g1", 100), "https://a/1.png")
        self.assertIsNone(cache.get_recent("g2", 100))

    def test_window_expired(self):
        cache = image_source.RecentImageCache()
        cache.record("g1", "https://a/1.png")
        self.assertIsNone(cache.get_recent("g1", -1))
        self.assertIsNone(cache.get_recent("g1", 0))

    def test_dedupe_and_cap(self):
        cache = image_source.RecentImageCache(max_per_session=2)
        cache.record("g1", "u1")
        cache.record("g1", "u2")
        cache.record("g1", "u1")
        cache.record("g1", "u3")
        # 最新的是 u3
        self.assertEqual(cache.get_recent("g1", 100), "u3")

    def test_clear(self):
        cache = image_source.RecentImageCache()
        cache.record("g1", "u1")
        cache.clear("g1")
        self.assertIsNone(cache.get_recent("g1", 100))


class TestComponentHelpers(unittest.TestCase):
    def test_is_image(self):
        self.assertTrue(image_source.is_image_component(FakeImage("https://a/1.png")))
        self.assertFalse(image_source.is_image_component(FakeReply("1")))

    def test_is_reply(self):
        self.assertTrue(image_source.is_reply_component(FakeReply("1")))
        self.assertFalse(image_source.is_reply_component(FakeImage()))

    def test_image_url(self):
        img = FakeImage("https://a/1.png")
        self.assertEqual(image_source.image_component_url(img), "https://a/1.png")
        local = FakeImage("file:///tmp/x.png")
        self.assertIsNone(image_source.image_component_url(local))
        self.assertIsNone(image_source.image_component_url(None))

    def test_extract_urls(self):
        comps = [FakeReply("1"), FakeImage("https://a/1.png"), FakeImage("file:///x"), FakeImage("https://a/2.png")]
        self.assertEqual(image_source.extract_urls_from_components(comps), ["https://a/1.png", "https://a/2.png"])

    def test_extract_reply_id(self):
        comps = [FakeImage(), FakeReply("999")]
        self.assertEqual(image_source.extract_reply_id(comps), "999")
        self.assertIsNone(image_source.extract_reply_id([FakeImage()]))


class TestResolveImage(unittest.TestCase):
    def test_current_message(self):
        event = FakeEvent([FakeImage("https://a/now.png")])
        url, src = run(image_source.resolve_image(event, image_source.RecentImageCache(), 600))
        self.assertEqual((url, src), ("https://a/now.png", "current"))

    def test_reply_chain(self):
        reply = FakeReply("1", chain=[FakeImage("https://a/reply.png")])
        event = FakeEvent([reply])
        url, src = run(image_source.resolve_image(event, image_source.RecentImageCache(), 600))
        self.assertEqual((url, src), ("https://a/reply.png", "reply_chain"))

    def test_reply_fetch_via_bot(self):
        bot = FakeBot([{"type": "text", "data": {"text": "hi"}}, {"type": "image", "data": {"url": "https://a/fetched.png"}}])
        event = FakeEvent([FakeReply("42")], bot=bot)
        url, src = run(image_source.resolve_image(event, image_source.RecentImageCache(), 600))
        self.assertEqual((url, src), ("https://a/fetched.png", "reply_fetch"))

    def test_reply_fetch_no_image(self):
        bot = FakeBot([{"type": "text", "data": {"text": "no image"}}])
        event = FakeEvent([FakeReply("42")], bot=bot)
        url, src = run(image_source.resolve_image(event, image_source.RecentImageCache(), 600))
        self.assertEqual((url, src), (None, "none"))

    def test_reply_fetch_bot_error(self):
        class BadBot:
            async def call_action(self, action, **kwargs):
                raise RuntimeError("api down")

        event = FakeEvent([FakeReply("42")], bot=BadBot())
        url, src = run(image_source.resolve_image(event, image_source.RecentImageCache(), 600))
        self.assertEqual((url, src), (None, "none"))

    def test_recent_cache(self):
        cache = image_source.RecentImageCache()
        cache.record("session:group:123", "https://a/recent.png")
        event = FakeEvent([], origin="session:group:123")
        url, src = run(image_source.resolve_image(event, cache, 600))
        self.assertEqual((url, src), ("https://a/recent.png", "recent"))

    def test_recent_expired(self):
        cache = image_source.RecentImageCache()
        cache.record("session:group:123", "https://a/recent.png")
        event = FakeEvent([], origin="session:group:123")
        url, src = run(image_source.resolve_image(event, cache, 0))
        self.assertEqual((url, src), (None, "none"))

    def test_no_image(self):
        event = FakeEvent([])
        url, src = run(image_source.resolve_image(event, image_source.RecentImageCache(), 600))
        self.assertEqual((url, src), (None, "none"))


class TestRecordEventImages(unittest.TestCase):
    def test_record(self):
        cache = image_source.RecentImageCache()
        event = FakeEvent([FakeImage("https://a/1.png"), FakeImage("https://a/2.png")])
        self.assertTrue(image_source.record_event_images(event, cache))
        self.assertEqual(cache.get_recent("session:group:123", 100), "https://a/2.png")

    def test_record_no_image(self):
        cache = image_source.RecentImageCache()
        event = FakeEvent([])
        self.assertFalse(image_source.record_event_images(event, cache))
        self.assertIsNone(cache.get_recent("session:group:123", 100))

    def test_record_session_key_fallback(self):
        cache = image_source.RecentImageCache()
        event = FakeEvent([FakeImage("https://a/1.png")])
        del event.unified_msg_origin
        event.session_id = "abc"
        self.assertTrue(image_source.record_event_images(event, cache))
        self.assertEqual(cache.get_recent("abc", 100), "https://a/1.png")


if __name__ == "__main__":
    unittest.main(verbosity=2)
