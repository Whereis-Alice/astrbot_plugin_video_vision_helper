# 更新日志

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
