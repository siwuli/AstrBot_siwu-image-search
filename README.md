# 动漫识图（image_search）

发图即可识图：截图找番、识别动漫角色、查找原图与画师，三个引擎由 LLM 自动路由。

## 功能
- trace.moe 截图找番：哪部番、第几集、第几秒
- AnimeTrace 识别图中角色与出处
- SauceNAO 查找原图、画师与 pixiv 链接
- 智能取图：图文同发、引用回复、先发图后发文字
- 中文标题优先展示，相似度阈值过滤

## 指令
- `识图`：默认找番
- `识图 角色`：识别角色（「人物」同样命中）
- `识图 来源`：找原图或画师（「画师/原图/出处」同样命中）
- Agent 对话无需指令：发图后直接提问即可自动路由

## 配置
| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `img_search_enabled` | true | 插件总开关 |
| `img_search_max_results` | 3 | 每个引擎最多返回条数 |
| `img_trace_similarity` | 0.82 | trace.moe 相似度阈值 |
| `img_sauce_similarity` | 55 | SauceNAO 相似度阈值 |
| `img_search_recent_window` | 10 | 最近图片匹配时间窗（分钟） |
| `img_search_saucenao_key` | 空 | SauceNAO API Key，可选 |
| `img_search_preview` | true | 命中时直发场景预览图 |

## 安装
1. 上传插件 zip 到 AstrBot 插件管理并启用
2. 无第三方依赖，三个引擎均可直接使用
3. SauceNAO 建议注册免费 Key 提升配额

## 版本记录
| 版本 | 说明 |
| --- | --- |
| 0.1.5 | 精简文档 |
| 0.1.4 | 三引擎识图与智能取图 |

## 项目地址
<https://github.com/siwuli/AstrBot_siwu-image-search>
