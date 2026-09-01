"""动漫识图插件（image_search，AstrBot）。

兔兔识图三合一，按「多工具 + LLM 路由」设计：不做一个猜引擎的万能入口，
而是拆成三个 Agent 工具，由 LLM 根据用户提问的语义自动选择：

- siwu_img_find_anime     → trace.moe   ：动漫截图帧级溯源（哪部番 / 第几集 / 第几秒）
- siwu_img_find_character → AnimeTrace  ：识别图内动漫/游戏角色
- siwu_img_find_source    → SauceNAO    ：找原图与画师来源（pixiv 等收录站）

取图（手机端图文分发的真实场景）：
1. 当前消息图文同发（Image 组件）；
2. 引用/回复一张带图消息后补文字（Reply.chain 或 get_msg 拉原消息）；
3. 先发图、后发文字（本插件全局过滤器把每条带图消息记入会话最近图片缓存）。
取图逻辑在 image_source.py（纯逻辑、可单测）。

规则（与模板一致）：
- Agent 工具只回传数据，最终回复由主 LLM 组织输出；
- 若命中预览图且配置允许，工具内 await event.send() 直发预览图后，
  再 yield 结构化数据 + 说明，主 LLM 据此收尾；此时 on_llm_request 已移除
  内置 send_message_to_user，避免重复发送。
"""

from __future__ import annotations

import logging

from astrbot.api import star
from astrbot.api.all import AstrBotConfig, AstrMessageEvent, llm_tool
from astrbot.api.event import MessageChain, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.message_components import Image as ComponentImage
from astrbot.api.provider import ProviderRequest

from .image_api import (
    anime_trace_search,
    saucenao_search,
    trace_moe_search,
)
from .image_source import (
    RecentImageCache,
    record_event_images,
    resolve_image,
)

logger = logging.getLogger("astrbot")

_IMG_CACHE = RecentImageCache(max_per_session=8)


class ImageRecordFilter(CustomFilter):
    """放行所有消息，让全局 handler 有机会把带图消息记入最近图片缓存。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return True


class ImageSearchPlugin(star.Star):
    def __init__(self, context, config: AstrBotConfig = None):
        super().__init__(context, config)
        self.config = config or {}

    # ------------------------------------------------------------------
    # 配置辅助
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default)
        return default if value is None else value

    def _enabled(self) -> bool:
        return bool(self._cfg("img_search_enabled", True))

    def _recent_window_seconds(self) -> float:
        minutes = float(self._cfg("img_search_recent_window", 10) or 0)
        return max(0.0, minutes * 60)

    # ------------------------------------------------------------------
    # 全局图片记录（先发图后发文字场景）：不 yield、不拦截，纯记录
    # ------------------------------------------------------------------
    @filter.custom_filter(ImageRecordFilter, priority=1)
    async def record_images(self, event: AstrMessageEvent):
        try:
            record_event_images(event, _IMG_CACHE)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[image_search] record images failed: {exc}")

    # ------------------------------------------------------------------
    # 取图辅助
    # ------------------------------------------------------------------
    async def _resolve(self, event: AstrMessageEvent) -> tuple[str, str]:
        """返回 (图片 URL 或 "", 来源描述)。来源可写进工具回传，增加可解释性。"""
        url, source = await resolve_image(event, _IMG_CACHE, self._recent_window_seconds())
        return (url or ""), source

    @staticmethod
    def _src_label(source: str) -> str:
        return {
            "current": "本次消息",
            "reply_chain": "引用消息",
            "reply_fetch": "引用消息",
            "recent": "该会话最近的图片",
            "none": "",
        }.get(source, source)

    async def _send_preview(self, event: AstrMessageEvent, preview_url: str) -> bool:
        """配置允许时直发预览图。返回是否已发送。"""
        if not bool(self._cfg("img_search_preview", True)):
            return False
        if not preview_url:
            return False
        try:
            await event.send(MessageChain([ComponentImage.fromURL(preview_url)]))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[image_search] send preview failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # 工具 1：trace.moe 截图找番
    # ------------------------------------------------------------------
    @llm_tool(name="siwu_img_find_anime")
    async def find_anime(self, event: AstrMessageEvent, note: str = ""):
        """识别动漫/番剧截图出自哪部作品（trace.moe 帧级溯源，可精确到集数与秒）。适用于用户发了动漫截图或录屏画面并询问「这是哪部番/哪个动画/第几集/这个画面出自哪里」。图片无需在文字中附带链接：插件会自动从当前会话获取（支持图文同发、引用回复带图、或先发图后再提问）；若同一条消息有多张图或需要指定某张图，请在 note 中说明。命中后插件可能已直接发送场景预览图，请基于返回的结构化数据组织最终回复，中文标题缺失时用原文标题并说明。

        Args:
            note(string): 用户对图片的补充说明或查询要求，可为空
        """
        if not self._enabled():
            yield "识图功能已在配置中关闭。"
            return
        url, source = await self._resolve(event)
        if not url:
            yield "没有找到可识别的图片：请直接发送图片、引用/回复带图的消息，或先发图后再提问。"
            return
        record_event_images(event, _IMG_CACHE)
        try:
            results = await trace_moe_search(url)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[image_search] trace.moe failed: {exc}")
            yield "识图服务暂时不可用，请稍后再试（trace.moe 请求失败）。"
            return
        threshold = float(self._cfg("img_trace_similarity", 0.82) or 0)
        max_results = max(1, min(5, int(self._cfg("img_search_max_results", 3) or 3)))
        hits = [r for r in results if (r["similarity"] or 0) >= threshold][:max_results]
        if not hits:
            top = results[0] if results else None
            hint = ""
            if top:
                hint = (
                    f"最高相似度 {top['similarity']:.2%} 低于阈值 {threshold:.2%}（可能是画质差/字幕遮挡/非动画截图）。"
                    f"参考候选：{top['title_zh'] or top['title_native'] or top['title_romaji'] or '未知'}"
                )
            yield f"未找到可信的番剧匹配。{hint}"
            return
        sent = await self._send_preview(event, hits[0].get("preview_image") or "")
        lines = [
            f"来源：{self._src_label(source)}；引擎：trace.moe",
            f"匹配到 {len(hits)} 条结果：",
        ]
        for i, hit in enumerate(hits, 1):
            title = hit["title_zh"] or hit["title_native"] or hit["title_romaji"] or hit["title_en"] or "未知"
            ep = hit.get("episode")
            ep_part = f"，第 {ep} 集" if ep not in (None, "", 0) else ""
            sec_part = ""
            if hit.get("from_sec") is not None:
                sec_part = f"，约 {float(hit['from_sec']):.0f} 秒处"
            lines.append(f"{i}. {title}{ep_part}{sec_part}（相似度 {hit['similarity']:.2%}）")
        lines.append("（预览图已发送）" if sent else "（可在配置中开启预览图直发）")
        yield "\n".join(lines)

    # ------------------------------------------------------------------
    # 工具 2：AnimeTrace 识别角色
    # ------------------------------------------------------------------
    @llm_tool(name="siwu_img_find_character")
    async def find_character(self, event: AstrMessageEvent, note: str = ""):
        """识别动漫/游戏图片中的角色身份与出处（AnimeTrace 视觉识别）。适用于用户发了角色图、立绘、同人图并询问「这个角色是谁/出自哪部作品/角色名字」。图片无需在文字中附带链接：插件会自动从当前会话获取（支持图文同发、引用回复带图、或先发图后再提问）；若同一张图有多个角色或需要指定第几个，请在 note 中说明。请基于返回数据组织最终回复，识别置信度低时如实告知用户可能需要更清晰的图。

        Args:
            note(string): 用户对图片的补充说明或查询要求，可为空
        """
        if not self._enabled():
            yield "识图功能已在配置中关闭。"
            return
        url, source = await self._resolve(event)
        if not url:
            yield "没有找到可识别的图片：请直接发送图片、引用/回复带图的消息，或先发图后再提问。"
            return
        record_event_images(event, _IMG_CACHE)
        try:
            results = await anime_trace_search(url)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[image_search] AnimeTrace failed: {exc}")
            yield f"角色识别服务暂时不可用：{exc}"
            return
        max_results = max(1, min(5, int(self._cfg("img_search_max_results", 3) or 3)))
        confident = [r for r in results if r.get("confident")]
        hits = confident[:max_results]
        ai_flag = ""
        if results and any(r.get("ai") for r in results):
            ai_flag = "（提示：该图片疑似 AI 生成）"
        if not hits and confident:
            yield f"识别到 {len(confident)} 个角色：{ai_flag}\n" + "\n".join(
                f"{i}. {h['character'] or '未知角色'} —— {h['anime'] or '未知作品'}"
                for i, h in enumerate(confident[:max_results], 1)
            )
            return
        if not hits:
            unsure = [r for r in results if not r.get("confident")]
            if unsure:
                yield "识别结果不确定（API 标记 not_confident），可能是画面模糊、人物过小或遮挡；建议换一张更清晰的图。" + "\n" + (
                    "\n".join(
                        f"候选：{h['character'] or '未知'} —— {h['anime'] or '未知作品'}"
                        for h in unsure[:max_results]
                    )
                    or ""
                )
            else:
                yield "未能识别图中角色（API 无匹配结果）。"
            return
        lines = [
            f"来源：{self._src_label(source)}；引擎：AnimeTrace{ai_flag}",
            f"识别到 {len(hits)} 条候选：",
        ]
        for i, hit in enumerate(hits, 1):
            char = hit["character"] or "未知角色"
            anime = hit["anime"] or "未知作品"
            lines.append(f"{i}. {char} —— {anime}")
        yield "\n".join(lines)

    # ------------------------------------------------------------------
    # 工具 3：SauceNAO 找原图/画师
    # ------------------------------------------------------------------
    @llm_tool(name="siwu_img_find_source")
    async def find_source(self, event: AstrMessageEvent, note: str = ""):
        """查找插画/美图的原图出处、画师与平台链接（SauceNAO，可定位 pxv 等收录站，相似度 0~100）。适用于用户发了插画/壁纸/同人图并询问「原图/出处/画师/谁画的/pixiv 链接」。图片无需在文字中附带链接：插件会自动从当前会话获取（支持图文同发、引用回复带图、或先发图后再提问）。相似度低于阈值可能是因为压缩/裁切/非收录站图片，请如实向用户说明。

        Args:
            note(string): 用户对图片的补充说明或查询要求，可为空
        """
        if not self._enabled():
            yield "识图功能已在配置中关闭。"
            return
        url, source = await self._resolve(event)
        if not url:
            yield "没有找到可识别的图片：请直接发送图片、引用/回复带图的消息，或先发图后再提问。"
            return
        record_event_images(event, _IMG_CACHE)
        api_key = str(self._cfg("img_search_saucenao_key", "") or "").strip() or None
        try:
            results = await saucenao_search(url, api_key=api_key)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[image_search] SauceNAO failed: {exc}")
            yield "识图服务暂时不可用，请稍后再试（SauceNAO 请求失败）。"
            return
        threshold = float(self._cfg("img_sauce_similarity", 55) or 0)
        max_results = max(1, min(5, int(self._cfg("img_search_max_results", 3) or 3)))
        hits = [r for r in results if (r["similarity"] or 0) >= threshold][:max_results]
        if not hits:
            top = results[0] if results else None
            hint = ""
            if top:
                hint = (
                    f"最高相似度 {top['similarity']:.0f} 低于阈值 {threshold:.0f}，"
                    f"参考：{top['title'] or top['index_name'] or '未知来源'}"
                )
            yield f"未找到可信的来源。{hint}"
            return
        lines = [
            f"来源：{self._src_label(source)}；引擎：SauceNAO",
            f"匹配到 {len(hits)} 条结果：",
        ]
        for i, hit in enumerate(hits, 1):
            title = hit["title"] or "未知作品"
            member = hit["member"] or "未知画师"
            pixiv = f"pixiv id {hit['pixiv_id']}" if hit.get("pixiv_id") else ""
            first_url = hit["urls"][0] if hit.get("urls") else ""
            lines.append(f"{i}. 《{title}》 by {member}（相似度 {hit['similarity']:.0f}）{pixiv} {first_url}".rstrip())
        yield "\n".join(lines)

    # ------------------------------------------------------------------
    # 意图钩子：检测识图意图时注入「按语义三选一」指令并移除直发工具
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def force_image_tool(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._enabled():
            return
        text = event.get_message_str() or ""
        intent_words = (
            "识图", "找番", "哪部番", "什么番", "哪个番", "哪一集", "找动漫",
            "截图出自", "这图出自", "角色是谁", "什么角色", "哪个角色", "谁画的",
            "画师", "pixiv", "图片来源", "原图", "搜图", "以图搜", "动漫截图",
        )
        if not any(word in text for word in intent_words):
            return
        req.system_prompt = (
            f"{req.system_prompt}\n\n"
            "[image_search 指令] 用户请求与识图/找番/识角色/找来源相关，请从以下工具中按语义选择其一调用"
            "（不要选错引擎）：\n"
            "1. siwu_img_find_anime——用户想找「动漫截图/番剧画面」出自哪部作品、第几集、什么时间点；\n"
            "2. siwu_img_find_character——用户想识别「图中角色」是谁、来自哪个作品；\n"
            "3. siwu_img_find_source——用户想找「插画/美图」的原图、画师、pixiv 等来源。\n"
            "工具自动从会话中获取图片，无需用户提供链接；工具只返回数据，最终回复由你（主 LLM）组织输出，"
            "禁止调用 send_message_to_user。"
        )
        if req.func_tool is not None:
            try:
                if "send_message_to_user" in req.func_tool.names():
                    req.func_tool.remove_tool("send_message_to_user")
            except Exception:  # noqa: BLE001, S110 - 工具集差异，移除失败不影响主流程
                pass

    # ------------------------------------------------------------------
    # 兜底命令（不依赖 LLM，同样自动取图）
    # ------------------------------------------------------------------
    @filter.command("识图")
    async def cmd_search(self, event: AstrMessageEvent):
        """兜底命令：识图 [类型]。类型：找番(默认)/角色/来源。"""
        if not self._enabled():
            yield event.make_result().message("识图功能已在配置中关闭。")
            return
        text = (event.get_message_str() or "").replace("识图", "", 1).strip()
        if "角色" in text or "人物" in text:
            kind = "character"
        elif any(w in text for w in ("来源", "画师", "原图", "pixiv", "出处")):
            kind = "source"
        else:
            kind = "anime"
        url, source = await self._resolve(event)
        if not url:
            yield event.make_result().message(
                "没有找到可识别的图片：请直接发送图片、引用/回复带图的消息，或先发图后再提问。"
            )
            return
        record_event_images(event, _IMG_CACHE)
        try:
            if kind == "character":
                results = await anime_trace_search(url)
                threshold = 0.0
                label = "角色识别"
                fmt = lambda h: f"{h['character'] or '未知角色'} —— {h['anime'] or '未知作品'}"
            elif kind == "source":
                api_key = str(self._cfg("img_search_saucenao_key", "") or "").strip() or None
                results = await saucenao_search(url, api_key=api_key)
                threshold = float(self._cfg("img_sauce_similarity", 55) or 0)
                label = "来源查找"
                fmt = lambda h: f"《{h['title'] or '未知作品'}》 by {h['member'] or '未知画师'}（相似度 {h['similarity']:.0f}）"
            else:
                results = await trace_moe_search(url)
                threshold = float(self._cfg("img_trace_similarity", 0.82) or 0)
                label = "番剧溯源"
                fmt = lambda h: (
                    f"{h['title_zh'] or h['title_native'] or h['title_romaji'] or '未知'}"
                    + (f"，第 {h['episode']} 集" if h.get("episode") not in (None, "", 0) else "")
                    + (f"，约 {float(h['from_sec']):.0f} 秒" if h.get("from_sec") is not None else "")
                    + f"（相似度 {h['similarity']:.2%}）"
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[image_search] {kind} search failed: {exc}")
            yield event.make_result().message(f"识图服务暂时不可用：{exc}")
            return
        max_results = max(1, min(5, int(self._cfg("img_search_max_results", 3) or 3)))
        if kind == "character":
            hits = [r for r in results if r.get("confident")][:max_results]
        else:
            hits = [r for r in results if (r.get("similarity") or 0) >= threshold][:max_results]
        if not hits:
            yield event.make_result().message(f"{label}未找到可信匹配（来源：{self._src_label(source)}）。")
            return
        if kind == "anime" and hits[0].get("preview_image"):
            await self._send_preview(event, hits[0]["preview_image"])
        lines = [f"{label}结果（来源：{self._src_label(source)}）："]
        lines += [f"{i}. {fmt(h)}" for i, h in enumerate(hits, 1)]
        yield event.make_result().message("\n".join(lines))

    async def terminate(self):
        """插件卸载/停用清理（本插件无后台任务）。"""
