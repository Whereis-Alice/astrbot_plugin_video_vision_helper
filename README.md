# Video Vision Helper

`astrbot_plugin_video_vision_helper` 是一个 AstrBot 插件，用来把视频附件转换成当前多模态请求更容易消费的输入内容。

- 抽取关键帧并注入当前请求的多模态内容
- 可选抽取音频并写入 `audio_urls`
- 可选执行 STT，把转写文本注入到提示中

这个插件是对原作者 Yanlyn 的 [astrbot_plugin_gif_vision_helper](https://github.com/Yanlyn/astrbot_plugin_gif_vision_helper) 思路的延伸版本，目标是让暂时没有原生视频输入能力的模型，也能更稳定地理解视频里的动作、场景变化、字幕和语音线索。

## 工作方式

AstrBot 当前的 `ProviderRequest` 没有 `video_urls` 字段，但 Core 会把普通视频附件转换成包含本地路径的提示文本。对于以 QQ 群文件发送的视频，插件会额外读取事件中的官方 `File` 组件；必要时再从 OneBot 原始消息取得下载地址。本插件会在 `on_llm_request` 阶段统一接管这些视频，然后自动执行下面的流程：

1. 用 `ffprobe` 探测视频流和音频流
2. 按策略决定是否分段处理长视频
3. 按策略抽取关键帧
4. 按策略抽取音频
5. 按配置决定是否做 STT
6. 把结果重新注入到当前请求

## 功能特性

- 支持普通视频附件、QQ 群文件视频，以及引用消息里的两类视频
- 支持 AstrBot 官方 `File` 组件，并可通过 OneBot `get_group_file_url` 解析只有 `file_id` / `busid` 的群文件
- 支持 `uniform` / `fixed_interval` / `head_tail` 三种抽帧策略
- 支持长视频分段，可在总预算内对整段视频做均匀取段或头尾取段
- 支持 `disabled` / `attach` / `stt` / `attach_and_stt` 四种音频模式
- 支持两种 STT 后端
- `astrbot_configured`：直接复用 AstrBot 已配置的 STT provider
- `openai_compatible`：使用插件内单独配置的兼容 OpenAI transcription 接口
- 支持通过 `stt_policy.astrbot_provider_id` 指定某个 AstrBot STT provider
- 支持单段转写长度和单视频总转写长度限制
- 支持 quoted video 远程下载体积限制与超时保护
- 支持单视频总处理时长上限，避免抽帧、抽音频和 STT 无限拖长
- 支持短视频帧数与长视频总帧数独立配置，避免长视频按分段数无限放大图片量
- 支持默认不将抽到的关键帧写入会话历史，避免下一轮对话重复携带上一次视频帧
- 支持单次请求图片张数上限和图片总体积预算，降低上游多模态模型压力
- 支持视频被跳过时注入明确兜底提示，方便模型知道用户确实发过视频
- 支持 `debug_logging` 调试开关，便于排查视频解析和 STT 问题
- 支持可选记录 STT 转写预览到 Debug 日志，排错时更容易确认音频是否识别正确
- 支持插件自己的 `cleanup_policy`，可兜底清理过期的插件临时文件
- 支持把说明提示注入到 `extra_user_content`、`prompt` 或 `system_prompt`
- 成功处理后可移除 AstrBot Core 注入的原始视频路径提示

## QQ 群文件视频

从 `0.4.5` 开始，视频即使以“群文件”而不是普通“视频消息”发送，也可以进入抽帧和 STT 流程。插件按以下顺序解析：

1. 读取 AstrBot 事件链中的 `File` 组件及其本地路径或 URL
2. 如果组件没有可用地址，从 OneBot 原始 `file` 消息段读取 `file_id`、`busid` 和 `group_id`
3. 调用 `get_group_file_url` 获取下载地址
4. 在下载体积和超时限制内下载到插件临时目录，再执行正常的视频处理

默认只把常见视频扩展名当作视频文件，普通压缩包、文档和其它群文件不会被插件下载。相关中文配置位于“远程视频与群文件下载策略”：

- `识别以群文件形式发送的视频`：默认开启
- `按文件识别的视频扩展名`：可用逗号、空格或分号分隔
- `群文件视频无文字时补充默认提问`：默认开启，防止已唤醒的纯文件请求在进入插件前被 Core 跳过
- `群文件视频默认提问内容`：默认是“请分析这个视频文件的内容。”
- `单个远程视频最大下载体积（MB）`：群文件视频与引用视频共用
- `远程视频下载超时（秒）`：群文件视频与引用视频共用

群文件消息仍需按 AstrBot 当前触发规则唤醒机器人，例如引用该群文件后 @ 机器人或附带唤醒词。对于“已经唤醒、但没有其它文字”的群文件视频请求，插件会在 `on_waiting_llm_request` 阶段补入默认提问，避免 AstrBot 4.25.1 在 `on_llm_request` 之前提前结束。插件不会主动响应群里所有未唤醒的文件上传通知。

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

## 长视频处理建议

从 `0.4.1` 开始，插件默认启用长视频分段，但会用“总预算”保护上游模型：

- 短视频默认最多抽取 `10` 帧：`frame_policy.max_frames_per_video = 10`
- 长视频默认最多抽取 `24` 帧：`frame_policy.max_long_video_frames_per_video = 24`
- 单次请求默认最多注入 `32` 张图片：`frame_policy.max_images_per_request = 32`
- 单次请求默认图片总体积预算为 `15 MB`：`frame_policy.max_total_frame_bytes_mb = 15.0`

重点：长视频的 `max_long_video_frames_per_video` 是整条视频的总帧数预算，不会按分段数倍增。例如长视频分成 3 段且长视频帧数填 `24`，插件会把 24 帧分配到各段，而不是抽 `10 * 3 = 30` 帧。

从 `0.4.1` 开始，长视频帧数会按片段时长加权分配。比如一个 30 秒片段和一个 5 秒片段，不再平均各拿一半帧数，而是长片段拿更多、短片段拿更少，避免短尾段被过密抽帧拖慢处理。

从 `0.4.2` 开始，如果抽帧 / 音频预算已经足够覆盖完整视频，插件会优先使用连续分段。例如 33 秒视频会规划成 `0-30s` 和 `30-33s`，而不是把第二个短片段重新放回开头附近造成重叠。

这样设计是为了兼顾 Gemini Flash、Kimi 多模态这类模型的实际承压情况：模型也许能接很多图，但聊天链路、网关、上下文预算和图片编码体积经常先成为瓶颈。默认策略选择偏稳，优先保证请求能送达、能被理解，而不是一上来堆满图片。

常见搭配：

- 想平均覆盖整条长视频：`segment_policy.selection_mode = uniform`
- 想优先看开头和结尾：`segment_policy.selection_mode = head_tail`
- 想保持顺序看片：`segment_policy.selection_mode = head_only`

几个容易一起调的项：

- `segment_policy.segment_duration_seconds`：每段看多长
- `segment_policy.max_segments_per_video`：最多看几段
- `frame_policy.max_video_duration_seconds`：抽帧总预算
- `frame_policy.max_long_video_frames_per_video`：长视频总抽帧数预算
- `frame_policy.max_images_per_request`：单次请求最终最多注入多少张图片
- `frame_policy.max_total_frame_bytes_mb`：单次请求最终注入图片的总体积预算
- `audio_policy.max_audio_duration_seconds`：音频 / STT 总预算

也就是说，启用分段后，不再只是“截前 N 秒”，而是在预算内把多个片段分散到整条视频里处理。

### 推荐档位

默认档适合多数群聊和普通视频：

- 短视频 `10` 帧
- 长视频 `24` 帧
- 单请求 `32` 图
- 图片预算 `15 MB`

如果你确认上游模型和网关都比较能扛，可以尝试增强档：

- 长视频 `32` 到 `48` 帧
- 单请求 `40` 到 `56` 图
- 图片预算 `16` 到 `25 MB`
- 单视频处理时长上限建议调到 `300` 秒以上

如果你遇到请求失败、响应变慢、模型遗漏文字或画面，可以先降这三个配置：

- `frame_policy.max_long_video_frames_per_video`
- `frame_policy.max_images_per_request`
- `frame_policy.max_total_frame_bytes_mb`

如果日志里出现 `processing time limit was reached at stage=frame_extraction`，说明瓶颈不在模型，而在本地 FFmpeg 抽帧耗时。可以优先确认 `ffmpeg_policy.frame_seek_mode = fast`，再提高 `runtime_policy.max_processing_seconds_per_video`，或者适当降低长视频总帧数。

默认情况下，插件会把抽到的关键帧作为临时 `ImageURLPart` 注入当前请求。本轮模型能看见这些帧，但 AstrBot 保存历史时会自动剥掉它们，所以下一轮不会继续带着上一轮视频帧。

如果你希望保留旧行为，让抽帧进入会话历史，可以把 `frame_policy.persist_sampled_frames_to_history = true`。这个开关只影响插件抽出来的帧，不影响用户原本手动发来的图片。

## 临时文件与清理策略

插件不会做持久视频缓存。处理过程中会在系统临时目录生成带有 `astrbot_plugin_video_vision_helper_` 前缀的临时文件，例如引用视频下载文件、抽取的 JPG 帧、临时 WAV 音频。

正常情况下，插件会把需要随请求传给模型的临时文件交给 AstrBot 的事件级临时文件追踪器，由 AstrBot 在请求生命周期结束后清理。对于 STT 后不需要附带给模型的音频，插件会立即删除。

`cleanup_policy` 是插件自己的兜底清理策略，主要处理异常退出、请求中断、平台下载中断或 AstrBot 没来得及清理时残留的过期文件。它只会删除系统临时目录里文件名以 `astrbot_plugin_video_vision_helper_` 开头、且超过保留时间的普通文件，不会清理 AstrBot 其它缓存，也不会扫描插件目录。

默认策略：

- `cleanup_policy.enabled = true`
- `cleanup_policy.cleanup_on_startup = true`
- `cleanup_policy.cleanup_after_request = true`
- `cleanup_policy.ttl_hours = 24`
- `cleanup_policy.min_cleanup_interval_minutes = 30`
- `cleanup_policy.max_files_per_run = 200`

如果你希望更激进地清理，可以把 `ttl_hours` 调小；如果你正在排查请求链路，建议保留默认 24 小时，这样临时文件不容易在刚处理完时被误删。

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

## Debug 日志

如果你需要排查视频路径、quoted message、STT 选路或提示注入问题，可以打开：

- `debug_logging = true`

打开后，插件会额外输出这些调试信息：

- 视频附件是从 Core 注入 marker 还是从事件链 fallback 收集到的
- quoted video 的 `file` / `path` / 候选本地路径
- 群文件的 `File` 组件识别结果、`file_id` / `busid` 是否存在，以及 OneBot 文件 URL 解析结果
- FFprobe 识别结果、抽帧时间点、音频提取结果
- AstrBot STT provider 的选路结果
- 可选输出 STT 转写文本预览
- 提示注入的位置和数量

注意：

- 这些是插件自己的 `debug` 级日志
- 如果你的 AstrBot 全局日志级别没有显示 `debug`，需要同时确认主程序日志设置允许输出 `debug`
- 转写预览可能包含用户语音内容，默认关闭；排错时可以同时打开 `debug_logging = true` 和 `stt_policy.log_transcript_preview = true`

## 关于 quoted video 报错

如果你看到类似下面的日志：

```text
Error processing quoted video attachment: not a valid file: 8703a2ebfa5f99dd29ceb59f1e7b2ffc.mp4
```

通常说明平台在“引用消息里的视频”组件上，只给了一个文件名或文件标识，没有给 AstrBot Core 可直接访问的本地绝对路径、`file:///` 路径或可下载 URL。

这会导致：

1. AstrBot Core 在生成视频 marker 时先报一条错
2. 插件 fallback 到事件链继续尝试解析
3. 如果 `video.path` 或临时目录里也找不到真实文件，就只能跳过

`0.2.1` 之后，插件会：

- 优先尝试 `path`、`file:///`、已有本地文件、AstrBot 临时目录候选路径
- 对 `aiocqhttp` / OneBot V11 引用视频，额外回查 reply 原始消息并尝试使用 `url` 下载视频
- 避免对 quoted video 再额外刷一条重复 warning
- 把更多细节放到 `debug_logging` 里，方便继续定位是哪个平台适配器返回了不完整媒体路径

需要说明的是：

- 第一条 `Core` 级别的报错来自 AstrBot 本体，不是插件打印的
- 插件可以尽量兜底并减少重复告警，但不能拦截那条已经在 Core 里发生的日志

## 安装方式

1. 将插件目录放到 `data/plugins/astrbot_plugin_video_vision_helper`
2. 安装依赖：`pip install -r requirements.txt`
3. 确认 `ffmpeg` 与 `ffprobe` 可执行
4. 重载插件或重启 AstrBot
5. 在插件配置面板中按需调整抽帧、音频、STT 和调试开关

## 配置概览

- `enabled`：插件总开关
- `debug_logging`：调试日志开关
- `ffmpeg_policy`：FFmpeg/FFprobe 路径、命令超时和抽帧 seek 模式
- `frame_policy`：视频数量限制、抽帧模式、短视频帧数、长视频总帧数、图片张数预算、图片总体积预算、缩放和抽帧总分析时长
- `frame_policy.persist_sampled_frames_to_history`：是否把抽到的关键帧写入会话历史
- `segment_policy`：长视频是否分段、取段方式、单段时长和分段数量
- `audio_policy`：音频模式、采样率、音频 / STT 总分析时长、STT 失败时是否回退为仅附带音频
- `stt_policy`：STT 后端选择、AstrBot provider 绑定、自定义转写接口、语言、单段转写长度、整条视频总转写长度限制和可选转写预览日志
- `hint_policy`：说明提示的注入位置，以及总览 / 单视频 / 分段转写 / 跳过视频模板
- `download_policy`：引用视频下载开关、群文件视频识别、视频扩展名、无文字默认提问、下载体积限制和超时
- `runtime_policy`：单视频总处理时长限制
- `cleanup_policy`：插件临时文件兜底清理开关、保留时长、清理间隔和单次清理数量

## 已知限制

- 这不是原生视频理解，而是“抽帧 + 可选音频 + 可选转写”的补偿方案
- 默认仍然偏向稳妥处理；长视频会分段覆盖，但会受到长视频总帧数、单请求图片数和图片总体积预算限制
- 即使启用分段，也仍然会受到帧数、音频总时长、转写长度、下载体积和总处理时长限制
- 如果视频超过下载限制、处理失败或图片预算不足，插件会注入一条跳过说明，但不会强行处理超出预算的视频
- 转写文本会按 `max_transcript_chars` 和 `max_total_transcript_chars` 双重限制，防止提示过长
- `cleanup_policy` 只清理本插件生成的过期临时文件，不负责清理 AstrBot Core、平台适配器或其它插件的缓存
- 如果平台适配器对引用视频只返回裸文件名而没有真实路径，AstrBot Core 仍可能先打印一条错误日志
- 群文件上传本身必须进入 AstrBot 的消息处理流程并触发 LLM 请求；只有 OneBot `group_upload` 通知、但没有消息请求时，`on_llm_request` 插件无法单独启动一次模型调用

## 更新日志

详见 [CHANGELOG.md](./CHANGELOG.md)
