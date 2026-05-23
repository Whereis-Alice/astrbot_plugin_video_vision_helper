# Video Vision Helper

`astrbot_plugin_video_vision_helper` 是一个 AstrBot 插件，用来把视频附件转换成当前多模态请求更容易消费的输入内容。

- 抽取关键帧并写入 `image_urls`
- 可选抽取音频并写入 `audio_urls`
- 可选执行 STT，把转写文本注入到提示中

这个插件是对 `astrbot_plugin_gif_vision_helper` 思路的延伸版本，目标是让暂时没有原生视频输入能力的模型，也能更稳定地理解视频里的动作、场景变化、字幕和语音线索。

## 工作方式

AstrBot 当前的 `ProviderRequest` 没有 `video_urls` 字段，但 Core 会把视频附件转换成包含本地路径的提示文本。本插件会在 `on_llm_request` 阶段接管这些视频路径，然后自动执行下面的流程：

1. 用 `ffprobe` 探测视频流和音频流
2. 按策略抽取关键帧
3. 按策略抽取音频
4. 按配置决定是否做 STT
5. 把结果重新注入到当前请求

## 功能特性

- 支持普通视频附件和引用消息里的视频附件
- 支持 `uniform` 与 `fixed_interval` 两种抽帧策略
- 支持 `disabled` / `attach` / `stt` / `attach_and_stt` 四种音频模式
- 支持两种 STT 后端：
- `astrbot_configured`：直接复用 AstrBot 已配置的 STT provider
- `openai_compatible`：使用插件内单独配置的兼容 OpenAI transcription 接口
- 支持通过 `stt_policy.astrbot_provider_id` 指定某个 AstrBot STT provider
- 支持把说明提示注入到 `extra_user_content`、`prompt` 或 `system_prompt`
- 成功处理后可移除 AstrBot Core 注入的原始视频路径提示

## 依赖

- `ffmpeg`
- `ffprobe`
- Python 依赖：`pip install -r requirements.txt`

插件默认直接调用系统 `PATH` 中的 `ffmpeg` 和 `ffprobe`。如果你的环境路径不同，可以在配置里单独指定。

## 推荐配置

### 模型本身支持音频

- `audio_policy.mode = attach`
- 或 `audio_policy.mode = attach_and_stt`

### 模型不支持音频

- `audio_policy.mode = stt`
- 再根据你的场景选择 `stt_policy.backend`

## STT 配置说明

### 方案一：直接复用 AstrBot 已配置的 STT 供应商

这是更推荐的接法，适合你已经在 AstrBot 面板里配置好了 Whisper、SenseVoice、Xinference 等 STT provider 的场景。

- `stt_policy.backend = astrbot_configured`
- `stt_policy.astrbot_provider_id = ""`

当 `astrbot_provider_id` 留空时，插件会优先跟随当前会话正在使用的 STT provider；如果当前会话没有独立配置，就回退到 AstrBot 默认 STT provider；再不行才会取第一个可用 provider。

如果你想强制绑定某个 AstrBot STT provider，可以填写：

- `stt_policy.backend = astrbot_configured`
- `stt_policy.astrbot_provider_id = whisper_selfhost`

### 方案二：使用插件自定义的兼容接口

适合你想让这个插件走独立的语音转写服务，而不影响 AstrBot 全局 STT 配置。

- `stt_policy.backend = openai_compatible`
- `stt_policy.endpoint = https://your-host/v1`
- `stt_policy.api_key = your-key`
- `stt_policy.model = your-model`

说明：

- `endpoint` 可以直接填完整的 `/audio/transcriptions`
- 也可以只填到 `/v1`，插件会自动补成转写端点
- `language`、`prompt`、`temperature` 都是可选增强项

## 安装方式

1. 将插件目录放到 `data/plugins/astrbot_plugin_video_vision_helper`
2. 安装依赖：`pip install -r requirements.txt`
3. 确认 `ffmpeg` 与 `ffprobe` 可执行
4. 重载插件或重启 AstrBot
5. 在插件配置面板中按需调整抽帧、音频和 STT 策略

## 配置概览

- `ffmpeg_policy`：FFmpeg/FFprobe 路径和命令超时
- `frame_policy`：视频数量限制、抽帧模式、帧数、缩放和分析时长
- `audio_policy`：音频模式、采样率、最大音频时长、STT 失败时是否回退为仅附带音频
- `stt_policy`：STT 后端选择、AstrBot provider 绑定、自定义转写接口、语言和转写长度限制
- `hint_policy`：说明提示的注入位置与模板

## 已知限制

- 这不是原生视频理解，而是“抽帧 + 可选音频 + 可选转写”的补偿方案
- 超长视频只会分析前一段内容，避免请求体和处理时延失控
- 转写文本会按 `max_transcript_chars` 截断，防止提示过长

## 更新日志

详见 [CHANGELOG.md](./CHANGELOG.md)
