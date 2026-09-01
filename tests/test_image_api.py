"""image_api 解析函数单元测试（无需 AstrBot 环境、无需网络）。

运行：
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PLUGIN_DIR)

import image_api


class TestParseTraceMoe(unittest.TestCase):
    def test_parse_basic(self):
        data = {
            "frameCount": 999,
            "result": [
                {
                    "anilist": {
                        "id": 154587,
                        "title": {
                            "romaji": "Bocchi the Rock!",
                            "english": "Bocchi the Rock!",
                            "native": "ぼっち・ざ・ろっく！",
                            "chinese": "孤独摇滚！",
                        },
                    },
                    "filename": "test.mkv",
                    "episode": 1,
                    "from": 183.03,
                    "to": 185.05,
                    "similarity": 0.9521,
                    "image": "https://media.trace.moe/image/x.png",
                    "video": "https://media.trace.moe/video/x.mp4",
                },
                {
                    "anilist": {
                        "id": 154588,
                        "title": {"romaji": "Another Anime"},
                    },
                    "episode": 2,
                    "from": 10.0,
                    "to": 12.0,
                    "similarity": 0.8113,
                },
            ],
        }
        out = image_api.parse_trace_moe(data)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title_zh"], "孤独摇滚！")
        self.assertEqual(out[0]["title_romaji"], "Bocchi the Rock!")
        self.assertEqual(out[0]["episode"], 1)
        self.assertEqual(out[0]["from_sec"], 183.03)
        self.assertEqual(out[0]["preview_image"], "https://media.trace.moe/image/x.png")
        self.assertGreater(out[0]["similarity"], out[1]["similarity"])
        self.assertEqual(out[1]["title_zh"], "")
        self.assertEqual(out[1]["title_romaji"], "Another Anime")

    def test_parse_garbage(self):
        self.assertEqual(image_api.parse_trace_moe(None), [])
        self.assertEqual(image_api.parse_trace_moe({}), [])
        self.assertEqual(image_api.parse_trace_moe({"result": "oops"}), [])
        self.assertEqual(image_api.parse_trace_moe({"result": [None, "x", 1]}), [])

    def test_parse_missing_title(self):
        data = {"result": [{"anilist": {"id": 1}, "similarity": 0.9}]}
        out = image_api.parse_trace_moe(data)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title_zh"], "")
        self.assertEqual(out[0]["title_romaji"], "")


class TestAnimeTraceModelChain(unittest.TestCase):
    def test_chain_default(self):
        chain = image_api.anime_trace_model_chain()
        self.assertEqual(chain[0], "anime")
        self.assertIn("pre_stable", chain)
        self.assertIn("anime_model_lovelive", chain)

    def test_chain_preferred_first_dedup(self):
        chain = image_api.anime_trace_model_chain("pre_stable")
        self.assertEqual(chain[0], "pre_stable")
        self.assertNotIn("pre_stable", chain[1:])
        self.assertIn("anime", chain)

    def test_no_duplicates(self):
        chain = image_api.anime_trace_model_chain("anime_model_lovelive")
        self.assertEqual(len(chain), len(set(chain)))


class TestParseAnimeTrace(unittest.TestCase):
    """基于 2026-09 实测返回结构：data[].character[] 列表 + not_confident 标记。"""

    def test_parse_basic(self):
        data = {
            "code": 0,
            "ai": False,
            "trace_id": "abc123",
            "data": [
                {
                    "box": [0.1, 0.2, 0.3, 0.4],
                    "box_id": "b1",
                    "not_confident": False,
                    "character": [
                        {"work": "ご注文はうさぎですか？", "character": "天々座理世"},
                        {"work": "ご注文はうさぎですか？", "character": "香風ターニャ"},
                    ],
                },
                {
                    "box": [0.5, 0.6, 0.7, 0.8],
                    "box_id": "b2",
                    "not_confident": True,
                    "character": [{"work": "未知作品", "character": "模糊角色"}],
                },
            ],
        }
        out = image_api.parse_anime_trace(data, model="anime")
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["model"], "anime")
        self.assertEqual(out[0]["character"], "天々座理世")
        self.assertEqual(out[0]["anime"], "ご注文はうさぎですか？")
        self.assertTrue(out[0]["confident"])
        self.assertEqual(out[1]["character"], "香風ターニャ")
        self.assertTrue(out[0]["confident"] and out[1]["confident"])
        self.assertFalse(out[2]["confident"])
        self.assertEqual(out[2]["character"], "模糊角色")

    def test_parse_single_dict_character(self):
        data = {"code": 0, "data": [{"not_confident": False, "character": {"work": "W", "character": "C"}}]}
        out = image_api.parse_anime_trace(data)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["anime"], "W")
        self.assertEqual(out[0]["character"], "C")

    def test_parse_empty(self):
        self.assertEqual(image_api.parse_anime_trace(None), [])
        self.assertEqual(image_api.parse_anime_trace({}), [])
        self.assertEqual(image_api.parse_anime_trace({"data": {}}), [])
        self.assertEqual(image_api.parse_anime_trace({"data": "x"}), [])
        self.assertEqual(image_api.parse_anime_trace({"data": [None, 1]}), [])
        self.assertEqual(image_api.parse_anime_trace({"data": [{"character": "notlist"}]}), [])


class TestParseSaucenao(unittest.TestCase):
    def test_parse_basic(self):
        data = {
            "header": {"status": 0, "results_returned": 2},
            "results": [
                {
                    "header": {"similarity": "96.52", "index_name": "Pixiv Images"},
                    "data": {
                        "title": "测试图",
                        "member_name": "画师A",
                        "pixiv_id": 123456,
                        "ext_urls": ["https://www.pixiv.net/artworks/123456"],
                    },
                },
                {
                    "header": {"similarity": "42.1", "index_name": "Danbooru"},
                    "data": {"title": "", "ext_urls": []},
                },
            ],
        }
        out = image_api.parse_saucenao(data)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "测试图")
        self.assertEqual(out[0]["member"], "画师A")
        self.assertEqual(out[0]["pixiv_id"], 123456)
        self.assertEqual(out[0]["urls"][0], "https://www.pixiv.net/artworks/123456")
        self.assertEqual(out[0]["index_name"], "Pixiv Images")
        self.assertGreater(out[0]["similarity"], out[1]["similarity"])
        self.assertEqual(out[1]["urls"], [])

    def test_parse_garbage(self):
        self.assertEqual(image_api.parse_saucenao(None), [])
        self.assertEqual(image_api.parse_saucenao({}), [])
        self.assertEqual(image_api.parse_saucenao({"header": {}}), [])
        self.assertEqual(image_api.parse_saucenao({"header": {"status": 0}, "results": [None, {}]}), [])


class TestIsUrl(unittest.TestCase):
    def test_is_url(self):
        self.assertTrue(image_api.is_url("https://example.com/a.png"))
        self.assertTrue(image_api.is_url("http://example.com/a.png"))
        self.assertFalse(image_api.is_url("base64://xxx"))
        self.assertFalse(image_api.is_url(b"bytes"))
        self.assertFalse(image_api.is_url(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
