# 兔兔 - 动漫识图（image_search）

AstrBot 识图插件，**三引擎合一、以 LLM 工具路由**：不做"猜引擎"的万能入口，
而是拆成三个 Agent 工具，由主 LLM 根据用户提问的语义自动选择对应引擎：

| 工具 | 引擎 | 适用提问 |
| --- | --- | --- |
| `siwu_img_find_anime` | [trace.moe](https://trace.moe) | 「这张截图是哪部番 / 第几集 / 第几秒」——动漫画面帧级溯源 |
| `siwu_img_find_character` | [AnimeTrace](https://ai.animedb.cn) | 「图里这个角色是谁 / 出自哪个作品」——角色识别 |
| `siwu_img_find_source` | [SauceNAO](https://saucenao.com) | 「原图 / 画师 / pixiv 链接」——插画原作溯源 |

> 插件 id：`image_search`　当前版本：`0.1.3`　适用 AstrBot `>=4.0.0`

## 智能取图（手机端图文分发的真实场景）

看图不需要把图片放进文字里，插件按优先级自动从会话取图：

1. **当前消息**图文同发（Image 组件）；
2. **引用/回复**一张带图消息后补文字（Reply 组件，必要时通过 OneBot `get_msg` 拉取原消息）；
3. **先发图、后发文字**：插件全局监听把每条带图消息记入会话最近图片缓存（默认 10 分钟窗口，可配）。

来源会附在结果里（「来源：本次消息 / 引用消息 / 该会话最近的图片」），便于理解与排查。

## 使用方式

- **Agent 对话**（推荐，自动路由）：发图后直接问
  - 「这是哪部番？第几集？」→ `siwu_img_find_anime`
  - 「这个角色是谁？」→ `siwu_img_find_character`
  - 「这张图的原图/画师是谁？」→ `siwu_img_find_source`
- **兜底命令**（不依赖 LLM）：`识图`（默认找番）、`识图 角色`、`识图 来源`

LLM 工具遵循本仓库约定：**工具只回传结构化数据，最终回复由主 LLM 组织**；
`on_llm_request` 命中识图意图时注入「三选一」指令并移除内置 `send_message_to_user`，
避免重复发送。trace.moe 命中且配置允许时，工具会先直发场景预览图再回传数据收尾。

## 配置项（_conf_schema.json）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `img_search_enabled` | true | 总开关 |
| `img_search_max_results` | 3 | 每个引擎最多返回候选数（1~5） |
| `img_trace_similarity` | 0.82 | trace.moe 相似度阈值（0~1），低于视为未命中 |
| `img_char_similarity` | 0.5 | AnimeTrace 置信度阈值（0~1） |
| `img_sauce_similarity` | 55 | SauceNAO 相似度阈值（0~100） |
| `img_search_recent_window` | 10 | 最近图片匹配时间窗（分钟），0 关闭 |
| `img_search_saucenao_key` | 空 | SauceNAO API Key（可选，注册免费；不填限流较严） |
| `img_search_preview` | true | trace.moe 命中时直发场景预览图 |

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `main.py` | 插件类：3 个 `@llm_tool`、意图钩子、兜底命令、全局图片记录过滤器 |
| `image_api.py` | 三引擎 HTTP 客户端 + 解析函数（解析与网络分离，可单测） |
| `image_source.py` | 多级取图：当前消息 → 引用回复 → 会话最近图片缓存（纯逻辑，可单测） |
| `tests/` | 28 项单元测试（无需 AstrBot 环境、无需网络） |

## 测试

```bash
python -m unittest discover -s tests -v
```

## 打包与安装

```bash
python build.py   # 产出 dist/siwu-image-search-<version>.zip
```

AstrBot 管理面板 → 插件管理 → 上传安装；或解压到服务器 `data/plugins/image_search` 后重启。

## 备注

- 三个引擎均免费无需 Key（均有限流，SauceNAO 建议注册 Key 提升配额）；
- AnimeTrace 返回结构可能与文档略有出入时，以实际请求结果为准（解析层已做容错）；
- 中文标题优先展示，缺失时回退原文标题（native/romaji），由主 LLM 自然组织；
- 本插件只上传图片到第三方识图服务做识别，不存储任何图片或用户数据。
