# 更新日志

## 0.2.2 - 2026-05-23

- 新增 aiocqhttp / OneBot V11 的 quoted video 原始 reply 回查兜底
- 当 `Reply.chain` 里的 `Video` 只剩裸文件名时，插件会主动调用 `get_msg` 再解析原始 video segment
- 如果原始 video segment 带有可访问的 `url`，插件会自动下载引用视频后继续处理
- 更新 README 与版本元数据

## 0.2.1 - 2026-05-23

- 新增 `debug_logging` 配置开关，支持输出更细的插件级 debug 日志
- 优化 quoted video 解析逻辑，优先尝试 `path`、`file:///`、已有本地文件和临时目录候选路径
- 避免在 quoted video 无法解析时重复输出插件 warning，减少和 AstrBot Core 的重复刷屏
- 新增 quoted video 报错说明与排障文档

## 0.2.0 - 2026-05-23

- 新增 `stt_policy.backend = astrbot_configured`，可直接复用 AstrBot 已配置的 STT provider
- 新增 `stt_policy.astrbot_provider_id`，支持显式绑定某个 AstrBot STT provider
- 保留 `openai_compatible` 自定义 STT 通道，便于插件独立接入外部转写服务
- 优化 STT 选择逻辑：优先跟随当前会话，其次使用 AstrBot 默认 provider，最后回退到首个可用 provider
- 更新 README、配置说明和插件元数据

## 0.1.0 - 2026-05-23

- 初始化 `astrbot_plugin_video_vision_helper`
- 新增视频附件转关键帧能力
- 新增可配置音频处理模式：`disabled` / `attach` / `stt` / `attach_and_stt`
- 新增 OpenAI-compatible STT 配置入口
- 新增提示注入策略与 FFmpeg 相关配置
