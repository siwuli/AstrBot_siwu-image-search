"""三个识图引擎的 HTTP 客户端与结果解析（image_api）。

引擎分工（由 LLM 按用户意图路由到不同工具）：
- trace.moe    : POST https://api.trace.moe/search —— 动漫截图帧级溯源（哪部番/第几集/第几秒），
                 支持 anilistInfo 拿多语言标题、cutBorders 自动裁黑边；免费无需 Key（有限流），
                 带 Key 走 x-trace-key 头提升配额；
- AnimeTrace   : POST https://api.animetrace.com/v1/search —— 识别图内动漫/游戏角色（model=anime 等），
                 支持 url / base64 / file 三种传图方式；免费无需 Key（有限流）；
- SauceNAO     : GET  https://saucenao.com/search.php —— 找原图与画师来源（pixiv 等收录站），
                 相似度为 0~100；免费注册 Key（默认 30 秒 1 次限流）。

约定：
- 图片入参统一为「公网 URL（str）或原始字节（bytes）」：URL 直传省流量，
  URL 无法访问时可先下载为 bytes 再走 multipart 上传；
- parse_* 解析函数与网络层分离，可脱离网络做单元测试。
"""

from __future__ import annotations

import logging
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover - 仅本地单测环境可能缺失，AstrBot 自带 aiohttp
    aiohttp = None  # type: ignore[assignment]

logger = logging.getLogger("astrbot")

TRACE_MOE_ENDPOINT = "https://api.trace.moe/search"
ANIME_TRACE_ENDPOINT = "https://api.animetrace.com/v1/search"
SAUCENAO_ENDPOINT = "https://saucenao.com/search.php"

# AnimeTrace 模型降级链：优先用配置/默认模型，服务端临时停用(如 anime 模型
# 曾返回 code=17737「本模型临时停止访问中」)时依次尝试后续可用模型
ANIME_TRACE_MODEL_FALLBACK = ("anime", "pre_stable", "anime_model_lovelive")

_HTTP_PREFIX = ("http://", "https://")
_TIMEOUT = 20

# 三个引擎（尤其 AnimeTrace/Cloudflare 与 SauceNAO）对无 UA 或库默认 UA 的请求
# 会拒绝/重置/超时，统一带浏览器风格 UA
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AstrBot-image-search/0.1.0",
}


def is_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_HTTP_PREFIX)


def _require_aiohttp() -> None:
    if aiohttp is None:
        raise RuntimeError("aiohttp 不可用（AstrBot 环境自带，本地测试请先 pip install aiohttp）")


async def download_image(url: str, timeout: int = _TIMEOUT) -> bytes | None:
    """下载图片为原始字节（用于 URL 不可直连时回退为 multipart 上传）。"""
    _require_aiohttp()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), headers=DEFAULT_HEADERS
        ) as session, session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
            logger.warning(f"[image_search] download {url} -> HTTP {resp.status}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[image_search] download {url} failed: {exc}")
    return None


# ---------------------------------------------------------------------------
# trace.moe —— 截图找番
# ---------------------------------------------------------------------------
async def trace_moe_search(
    image: Any,
    timeout: int = _TIMEOUT,
    api_key: str | None = None,
    anilist_info: bool = True,
    cut_borders: bool = True,
) -> list[dict]:
    headers = dict(DEFAULT_HEADERS)
    if api_key:
        headers["x-trace-key"] = api_key
    params: dict[str, Any] = {}
    if anilist_info:
        params["anilistInfo"] = ""
    if cut_borders:
        params["cutBorders"] = "true"

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout), headers=headers
    ) as session:
        if is_url(image):
            params["url"] = image
            async with session.post(TRACE_MOE_ENDPOINT, params=params, headers=headers) as resp:
                data = await resp.json(content_type=None)
        else:  # bytes：multipart 上传
            async with session.post(
                TRACE_MOE_ENDPOINT, params=params, headers=headers, data={"file": image}
            ) as resp:
                data = await resp.json(content_type=None)
    return parse_trace_moe(data)


def parse_trace_moe(data: Any) -> list[dict]:
    """解析 trace.moe 返回：按相似度降序输出候选列表。"""
    out: list[dict] = []
    if not isinstance(data, dict):
        return out
    for item in (data.get("result") or []):
        if not isinstance(item, dict):
            continue
        anilist = item.get("anilist")
        title_raw = {}
        if isinstance(anilist, dict):
            raw_title = anilist.get("title")
            if isinstance(raw_title, dict):
                title_raw = {str(k): str(v) for k, v in raw_title.items() if v}
        out.append({
            "engine": "trace.moe",
            "title_zh": title_raw.get("chinese") or title_raw.get("zh") or "",
            "title_native": title_raw.get("native") or "",
            "title_romaji": title_raw.get("romaji") or "",
            "title_en": title_raw.get("english") or title_raw.get("en") or "",
            "anilist_id": (anilist or {}).get("id") if isinstance(anilist, dict) else None,
            "episode": item.get("episode"),
            "from_sec": item.get("from"),
            "to_sec": item.get("to"),
            "similarity": float(item.get("similarity") or 0),
            "preview_image": item.get("image"),
            "preview_video": item.get("video"),
        })
    out.sort(key=lambda x: -(x["similarity"] or 0))
    return out


# ---------------------------------------------------------------------------
# AnimeTrace —— 识角色
# ---------------------------------------------------------------------------
def anime_trace_model_chain(preferred: str = "anime") -> list[str]:
    """返回模型尝试顺序：首选 + 降级链（去重、保持顺序）。"""
    chain = [preferred]
    for model in ANIME_TRACE_MODEL_FALLBACK:
        if model not in chain:
            chain.append(model)
    return chain


async def _anime_trace_post(
    session: Any,
    image: Any,
    model: str,
    ai_detect: bool,
) -> tuple[dict | None, str | None]:
    """请求 AnimeTrace 一次；返回 (data, 错误消息)，成功时错误消息为 None。"""
    params: dict[str, Any] = {"model": model}
    if ai_detect:
        params["ai_detect"] = "1"
    data: Any = None
    try:
        async with session.post(
            ANIME_TRACE_ENDPOINT,
            params=params,
            json={"url": image} if is_url(image) else None,
            data=None if is_url(image) else {"file": image},
        ) as resp:
            data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 - 网络层失败计入降级链
        return None, str(exc)
    if isinstance(data, dict) and data.get("code") not in (0, 200, None) and data.get("data") == {}:
        msg = data.get("zh_message") or data.get("message") or f"code={data.get('code')}"
        return None, str(msg)
    return data, None


async def anime_trace_search(
    image: Any,
    timeout: int = _TIMEOUT,
    model: str = "anime",
    ai_detect: bool = False,
) -> list[dict]:
    """识别角色；模型不可用时沿降级链自动换模型，全部失败抛 RuntimeError。"""
    errors: list[str] = []
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout), headers=DEFAULT_HEADERS
    ) as session:
        for candidate in anime_trace_model_chain(model):
            data, err = await _anime_trace_post(session, image, candidate, ai_detect)
            if data is not None:
                return parse_anime_trace(data, model=candidate)
            errors.append(f"{candidate}: {err}")
    raise RuntimeError(f"AnimeTrace 所有模型均不可用：{' | '.join(errors)}")


def parse_anime_trace(data: Any, model: str = "") -> list[dict]:
    """解析 AnimeTrace 返回（实测结构，2026-09 验证）。

    真实返回样例：
        {"code": 0, "ai": false, "trace_id": "...",
         "data": [{"box": [...], "box_id": "...", "not_confident": false,
                   "character": [{"work": "作品名", "character": "角色名"}, ...]}]}

    注意：
    - 每个 data 条目是一个「检测到的人物框」，可包含多个角色候选 character[];
    - API 不返回置信度分数，只提供 not_confident 布尔标记（false=可信）；
    - code 0/200 表示成功；上限次数或失败时 data 可能是空/错误说明。
    """
    out: list[dict] = []
    if not isinstance(data, dict):
        return out
    raw = data.get("data")
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        chars = item.get("character")
        conf = not bool(item.get("not_confident"))
        if isinstance(chars, dict):
            chars = [chars]
        if isinstance(chars, list):
            for c in chars:
                if not isinstance(c, dict):
                    continue
                out.append({
                    "engine": "anime_trace",
                    "model": model,
                    "character": str(c.get("character") or c.get("name") or ""),
                    "anime": str(c.get("work") or c.get("anime") or c.get("作品") or ""),
                    "confident": conf,
                    "ai": bool(item.get("ai")),
                    "trace_id": data.get("trace_id"),
                })
    # 可信候选在前，输入顺序保持（API 已按检测置信排序）
    out.sort(key=lambda x: (0 if x["confident"] else 1))
    return out


# ---------------------------------------------------------------------------
# SauceNAO —— 找原图/画师
# ---------------------------------------------------------------------------
async def saucenao_search(
    image: Any,
    api_key: str | None = None,
    timeout: int = _TIMEOUT,
    db: int = 999,
) -> list[dict]:
    params: dict[str, Any] = {"db": str(db), "output_type": "2"}
    if api_key:
        params["api_key"] = api_key

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout)
    ) as session:
        if is_url(image):
            params["url"] = image
            async with session.get(SAUCENAO_ENDPOINT, params=params) as resp:
                data = await resp.json(content_type=None)
        else:
            async with session.post(
                SAUCENAO_ENDPOINT, params=params, data={"file": image}
            ) as resp:
                data = await resp.json(content_type=None)
    return parse_saucenao(data)


def parse_saucenao(data: Any) -> list[dict]:
    """解析 SauceNAO 返回：按相似度(0~100)降序输出候选列表。"""
    out: list[dict] = []
    if not isinstance(data, dict):
        return out
    header = data.get("header")
    if not isinstance(header, dict):
        return out
    for item in (data.get("results") or []):
        if not isinstance(item, dict):
            continue
        item_header = item.get("header")
        item_data = item.get("data")
        if not isinstance(item_header, dict) or not isinstance(item_data, dict):
            continue
        try:
            similarity = float(item_header.get("similarity") or 0)
        except (TypeError, ValueError):
            similarity = 0.0
        ext_urls = item_data.get("ext_urls") or []
        out.append({
            "engine": "saucenao",
            "similarity": similarity,  # 0~100
            "index_name": str(item_header.get("index_name") or ""),
            "title": str(item_data.get("title") or item_data.get("jp_name") or ""),
            "member": str(item_data.get("member_name") or item_data.get("author") or ""),
            "pixiv_id": item_data.get("pixiv_id"),
            "urls": [str(u) for u in ext_urls if isinstance(u, str)][:5],
        })
    out.sort(key=lambda x: -(x["similarity"] or 0))
    return out
