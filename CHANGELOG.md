# 更新日志

## 0.3.1 - 2026-05-24

- 将 `frame_policy.max_frames_per_video` 的默认值从 `6` 提升到 `10`
- 保持抽帧策略与分段逻辑可配置不变，但默认覆盖信息量更充分
- 更新 README 与插件元数据

## 0.3.0 - 2026-05-24

- 新增 `segment_policy`，支持长视频分段处理，可按 `head_only` / `uniform` / `head_tail` 选择取段策略
- 新增 `frame_policy.sampling_mode = head_tail`，抽帧时可优先照顾开头和结尾镜头
- 优化长视频分析逻辑：抽帧和音频 / STT 各自尊重自己的总分析时长预算，不再简单只截前一段
- 新增 `stt_policy.max_total_transcript_chars`，支持限制单个视频全部分段转写的总长度
- 优化提示注入结构，新增单视频说明模板和分段转写模板
- 新增 `download_policy`，可限制 quoted video 远程下载开关、最大体积与超时
- 新增 `runtime_policy.max_processing_seconds_per_video`，可限制单个视频总处理耗时
- 优化 quoted video 下载实现，下载阶段会校验 `content-length` 和实际流式下载体积
- 修复事件链 fallback 中 quoted video 可能重复收集的问题
- 更新 README、配置 schema 与插件元数据

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
