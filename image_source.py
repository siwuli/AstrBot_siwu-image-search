"""多级智能取图（image_source）：当前消息 → 引用回复 → 会话最近图片。

设计背景（手机端 QQ 常见场景）：
- 图文无法混发，用户通常「先发图片，再发文字」；
- 或「引用/回复一张图片消息，再补一句文字」。
因此单看当前事件的消息链远远不够，需三级回退：

1. current     当前事件消息链中的 Image 组件（图文混发 / 命令+图同发）；
2. reply_chain 引用回复组件 Reply.chain 内已解析出的 Image（部分平台/适配器会填充）；
3. reply_fetch 引用回复的 Reply.id → 通过平台 bot.get_msg(message_id) 拉取原消息
               （aiocqhttp/OneBot v11 的 CQHttp.call_action("get_msg", ...)），筛 image 段；
4. recent      本插件维护的「会话最近图片缓存」（模块级 _IMG_CACHE 由插件全局
               消息过滤器持续记录），匹配时间窗内的最近一张图。

本模块刻意不 import astrbot 运行时（鸭子类型判断组件），以便脱离 AstrBot
环境进行单元测试（tests/test_image_source.py）。
"""

from __future__ import annotations

import time
from typing import Any

_HTTP_PREFIX = ("http://", "https://")


class RecentImageCache:
    """按会话（unified_msg_origin）记录最近图片 URL，供「先图后文」场景回退。"""

    def __init__(self, max_per_session: int = 5) -> None:
        self._data: dict[str, list[tuple[float, str]]] = {}
        self._max = max(1, max_per_session)

    def record(self, session_key: str, url: str) -> None:
        if not session_key or not url:
            return
        now = time.time()
        lst = [x for x in self._data.get(session_key, []) if x[1] != url]
        lst.append((now, url))
        self._data[session_key] = lst[-self._max:]

    def get_recent(self, session_key: str, window_seconds: float) -> str | None:
        """返回该会话最近 window_seconds 秒内的图片 URL；无则 None。"""
        if not session_key or window_seconds <= 0:
            return None
        now = time.time()
        for ts, url in reversed(self._data.get(session_key, [])):
            if now - ts <= window_seconds:
                return url
        return None

    def clear(self, session_key: str) -> None:
        self._data.pop(session_key, None)


def is_image_component(comp: Any) -> bool:
    """鸭子类型判断 Image 组件（不依赖 astrbot 导入，便于单测）。"""
    if comp is None:
        return False
    type_name = type(comp).__name__.lower()
    type_tag = str(getattr(comp, "type", "") or "").lower()
    return "image" in type_name or "image" in type_tag


def is_reply_component(comp: Any) -> bool:
    """鸭子类型判断 Reply 组件：类型名/type 含 reply 且带 id 字段。"""
    if comp is None:
        return False
    type_name = type(comp).__name__.lower()
    type_tag = str(getattr(comp, "type", "") or "").lower()
    return ("reply" in type_name or "reply" in type_tag) and hasattr(comp, "id")


def image_component_url(comp: Any) -> str | None:
    """从 Image 组件提取可直连的公网 URL（url / file / path 依次尝试）。"""
    for attr in ("url", "file", "path"):
        value = getattr(comp, attr, None)
        if isinstance(value, str) and value.startswith(_HTTP_PREFIX):
            return value
    return None


def extract_urls_from_components(components: Any) -> list[str]:
    """从一段组件列表提取所有公网图片 URL（保持顺序，不够时截断）。"""
    urls: list[str] = []
    for comp in components or []:
        if is_image_component(comp):
            url = image_component_url(comp)
            if url:
                urls.append(url)
    return urls[:10]


def extract_reply_id(components: Any) -> str | None:
    """从组件列表提取第一个引用回复的消息 id。"""
    for comp in components or []:
        if is_reply_component(comp):
            rid = getattr(comp, "id", None)
            if rid not in (None, "", 0):
                return str(rid)
    return None


def _event_components(event: Any) -> list[Any]:
    """兼容取法取出事件的消息组件列表（AstrBot get_messages() / message_obj.message）。"""
    collect = getattr(event, "get_messages", None)
    if callable(collect):
        try:
            return list(collect()) or []
        except Exception:  # noqa: BLE001, S110 - 防御性回退：get_messages 不可用时走 message_obj
            pass
    message_obj = getattr(event, "message_obj", None)
    chain = getattr(message_obj, "message", None)
    return list(chain) if chain else []


async def _fetch_reply_image_via_bot(event: Any, reply_id: str) -> str | None:
    """aiocqhttp/OneBot v11：用 Reply.id 调 get_msg 拉原消息，筛 image 段公网 URL。"""
    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None)
    if not callable(call_action):
        return None
    try:
        message_id = int(reply_id) if str(reply_id).isdigit() else str(reply_id)
        data = await call_action("get_msg", message_id=message_id)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    for seg in data.get("message", []) or []:
        if not isinstance(seg, dict):
            continue
        if str(seg.get("type", "")).lower() != "image":
            continue
        seg_data = seg.get("data") or {}
        if not isinstance(seg_data, dict):
            continue
        for key in ("url", "file", "path"):
            value = seg_data.get(key)
            if isinstance(value, str) and value.startswith(_HTTP_PREFIX):
                return value
    return None


def session_key_of(event: Any) -> str:
    """取会话唯一标识（unified_msg_origin 优先，session_id 兜底）。"""
    origin = getattr(event, "unified_msg_origin", None)
    if origin:
        return str(origin)
    session_id = getattr(event, "session_id", None)
    return str(session_id) if session_id else ""


def record_event_images(event: Any, cache: RecentImageCache) -> bool:
    """把当前事件消息链中的图片 URL 记录进缓存，供后续「先图后文」回退。返回是否记录。"""
    if cache is None:
        return False
    urls = extract_urls_from_components(_event_components(event))
    if not urls:
        return False
    key = session_key_of(event)
    for url in urls:
        cache.record(key, url)
    return True


async def resolve_image(event: Any, cache: RecentImageCache, window_seconds: float) -> tuple[str | None, str]:
    """多级取图：current → reply_chain → reply_fetch → recent。

    Returns:
        (图片公网 URL 或 None, 来源标识: current/reply_chain/reply_fetch/recent/none)
    """
    components = _event_components(event)

    # 1. 当前消息中的图片（图文同行）
    urls = extract_urls_from_components(components)
    if urls:
        return urls[0], "current"

    # 2. 引用回复：适配器已解析出的被引用图片
    for comp in components:
        if is_reply_component(comp):
            chain = getattr(comp, "chain", None)
            reply_urls = extract_urls_from_components(chain)
            if reply_urls:
                return reply_urls[0], "reply_chain"

    # 3. 引用回复：get_msg 拉取原消息（aiocqhttp）
    reply_id = extract_reply_id(components)
    if reply_id:
        url = await _fetch_reply_image_via_bot(event, reply_id)
        if url:
            return url, "reply_fetch"

    # 4. 会话最近图片缓存（先发图后发文字）
    key = session_key_of(event)
    recent = cache.get_recent(key, window_seconds) if cache else None
    if recent:
        return recent, "recent"

    return None, "none"
