# 涂鸦音乐技能背景与设备实现

本文供后续维护和扩展音乐功能时参考，说明云端为什么会通过不同通道下发技能卡片、设备如何理解卡片，以及当前支持范围。音乐技能不是把音乐作为对话 TTS 音频发送下来，而是下发资源 URL 或播放控制指令，由设备自己的播放器执行。

核心原则是：保留 AI 文本流和 IoT MQTT 各自的传输语义，提取技能卡片后共用业务处理。不要根据“云端说了即将播放”判断播放成功，也不要根据歌曲是否为第三方内容，在设备端固定选择一个消息入口。

本文按 2026 年 9 月 30 日的本地代码整理：设备音乐实现以 `e986362` 为基线；云端参考 Polysense `652c86ef6`；TuyaOpen 参考 `5112368a`。云端部分描述的是这些代码中的实现，不代表所有部署环境都已启用相同能力。

## 云端技能如何产生播放结果

Polysense 的 `PlayMusicService.handleIntent` 查询音乐资源并获取播放地址。其返回或推送的业务内容包括技能码、播放动作、音频元数据和是否等待前置 TTS。

- 非第三方资源路径把 `audios` 放到返回的技能卡片中，供 AI 链路传给设备。
- 第三方资源路径存在延迟 2 秒启动异步任务的实现：返回结果中的 `skillCard` 置空，之后由 `doPushMQTT` 获取资源地址，通过 `Protocol9000MessageDTO.newSkillInstance` 和 `AigcMqttMessagePushUtil.pushAigcMessage4Device` 推送卡片。旧的 `MusicToolService` 也有相同路径。2 秒是云端任务调度延迟，不是消息到达的保证。
- 播控技能会返回 `PlayControl` 卡片，也有 MQTT 请求处理和下行推送路径。暂停、继续等动作可以没有音频 URL；它们操作的是设备已有的播放状态。

因此一次语音请求可能先出现 ASR、NLG 和 TTS，稍后才出现 MQTT 音乐卡片。MQTT 的 `bizId` 在上述推送代码中单独生成，不保证等于 AI 对话的 `vcd-event`。定位问题时需要通过云端的 `requestId`、设备标识和时间关联，不能只拿两侧 `bizId` 做相等匹配。

资源授权和曲库选择由云端决定，固件收到 URL 后不能推断其版权、订购状态或是否为完整歌曲。正式产品需要另外确认平台配置及内容授权。

## 两条消息入口和统一播放处理

```text
AI TAI 文本回调 → TuyaTextStream 重组 → bizType SKILL → type skill ─┐
                                                                 ├→ HandleTuyaMusicSkill
IoT MQTT 回调 → 有界投递到主任务 → 解包并选择技能卡片 ─────────────┘
    → MusicPlayer → HTTP 下载 MP3 → 解码与重采样 → 音乐 PCM 队列 → 扬声器
```

| 入口 | 传输内容 | 设备处理 |
| --- | --- | --- |
| AI 链路 | `on_text` 中的业务 JSON，可以是一条完整 JSON，也可以是分片 | `TuyaProtocol::HandleText` 用 `TuyaTextStream` 收齐一个 JSON 对象，再将 `SKILL.data` 转为应用内的 `type=skill` |
| IoT MQTT | SDK 应用回调收到的 JSON 消息，常见外层为 `protocol=9000` | `OnMqttMessage` 复制消息并投递到主任务；`HandleTuyaMqttMessage` 解包，`SelectTuyaMqttSkillCard` 选择卡片 |

两个入口最后都调用 `AudioService::HandleTuyaMusicSkill`，由 `MusicPlayer::HandleSkillCard` 执行动作。MQTT 不伪装成 TAI 的流式 `on_text`，也不经过设备 MCP 的 JSON RPC 工具调用分发。

云端内部可能通过工具或 MCP 触发音乐技能，但“云端工具调用方式”和“设备收到卡片的通道”是两个概念。对话 TTS 经 `on_audio` 播放；音乐文件经独立 HTTP 下载播放，不能把 MP3 文件或 HTTP 分片当成对话 Opus 帧。

IoT client 必须持续运行 MQTT 接收循环，不能在取得 AI session token 后立即销毁。本项目已有 `MqttPumpLoop` 负责接收、连接维护及云端重置通知，音乐复用应用消息回调，不替换这些生命周期处理。代码中关于取 token 后释放 IoT client 的旧注释不代表实际行为，当前销毁调用处于注释状态。

## 可选自动下一首

在 `idf.py menuconfig` 的 Xiaozhi 配置中启用
`Automatically request the next Tuya music track`（`CONFIG_TUYA_MUSIC_AUTO_NEXT=y`）。
默认关闭，依赖 `CONFIG_PROTOCOL_TUYA`；这是编译期开关，不是 App/技能平台上的自动连播开关。

开启后，一张音乐卡片的有效 MP3 列表全部成功播放且输出队列排空，设备才请求云端下一首。
列表内多首仍先顺序播放，只在列表耗尽时请求一次。故事、失败、取消和暂停不触发。
用户开始新对话、停止音乐或替换列表会使尚未消费的完成通知失效；暂停后显式续播并自然播完仍可继续。

请求交由现有 MQTT pump 发送，不从音乐工作线程或主任务并发调用 SDK publish：

```json
{"protocol":9000,"t":1790000000,"data":{"bizId":"music-next-example","bizType":"SKILL","data":{"code":"PlayControl","action":"next","auto":"true"}}}
```

格式对应 Polysense `SkillTopicConsumer` / `MqttBody` / `SkillBody`：参数在 `data.data`，
`auto` 使用字符串；它不同于云端下行卡片的 `general.data`。网关根据设备上行 topic 提供设备身份，
SDK 的 `iot_client_publish()` 负责加密和发送。云端必须已关联播控技能，并允许该设备的 MQTT 播控；
仅打开固件开关不保证云端会返回下一首。

MQTT 待发布请求最多一条，每次播放完成只消费一次通知。30 秒未收到有效新播放卡片时打印超时并停止等待；
MQTT pump 在实际发送前重新核验播放 generation 与截止时间，丢弃停止、暂停、新对话或超时后尚未发送的请求。
发布失败及超时均不自动重试，因为 `next` 会推进云端歌单，重试可能跳过歌曲。
响应复用现有 `PlayControl/action=next/audios` 处理；不会自动发文本请求或要求大模型回复。
日志可按 `automatic next queued` → `Automatic music next request published` →
`Tuya MQTT skill accepted=1` → `Queued music playlist` 排查。

当前云端响应会重新生成 `bizId`，没有原请求 ID 的可靠回显。因此无法严格识别取消或超时后迟到的自动响应，
迟到的合法卡片仍按现有下行逻辑处理；若产品要求严格取消，应先给云端协议增加请求关联字段。
本轮不实现循环/随机策略，也不修改云端代码。

## 技能卡片格式

以下 JSON 是用于说明结构的简化示例，URL 和标识均为占位值，不是可直接播放的资源。

### AI 链路业务 JSON

```json
{
  "bizId": "example-ai-event",
  "bizType": "SKILL",
  "eof": 1,
  "data": {
    "code": "music",
    "general": {
      "template": { "name": "audio", "version": "1.0" },
      "action": "play",
      "data": {
        "preTtsFlag": true,
        "audios": [
          {
            "id": 0,
            "audioId": "example-audio",
            "name": "示例歌曲",
            "artist": "示例歌手",
            "album": "示例专辑",
            "imageUrl": "https://example.com/cover.jpg",
            "duration": 0,
            "format": "mp3",
            "url": "https://example.com/music.mp3"
          }
        ]
      }
    },
    "custom": { "data": {} }
  }
}
```

日志里还可能有 `packet-type=text`、`stream-flag`、`seq` 等传输层外壳，其中 `payload.data` 是带转义的 JSON 字符串。TAI 回调处理的是业务文本，不是把整行日志直接传给播放器。当前重组器看到完整 JSON 就处理，即使所在帧标记为 `middle`；不要机械等待整轮对话 `end`，也不要将整轮的 ASR、NLG、SKILL 拼成一份 JSON。

### MQTT 9000 下行

```json
{
  "protocol": 9000,
  "s": 1,
  "t": 0,
  "data": {
    "bizId": "example-mqtt-event",
    "bizType": "SKILL",
    "data": {
      "code": "music",
      "general": {
        "template": { "name": "audio", "version": "1.0" },
        "action": "play",
        "data": {
          "preTtsFlag": true,
          "audios": [
            { "format": "mp3", "url": "https://example.com/music.mp3" }
          ]
        }
      },
      "custom": { "data": {} }
    }
  }
}
```

这是当前解析器和主机测试覆盖的外壳，云端通过 9000 消息构造器生成业务推送。`s`、`t` 不参与本项目的音乐分发；若存在 `protocol` 字段，必须是数值 `9000`。普通 MQTT 消息、其他协议或 `bizType=NLG` 不应仅因包含 `code` 字段而被当作音乐卡片。

SDK 应用回调中的内容也可能已经去掉外壳。当前兼容以下形式：

- `{"bizType":"SKILL","data":卡片}`。
- `{"skillCard":卡片}`。
- 直接的卡片对象，即根对象包含字符串 `code`。
- `packet-type=text` 外壳中的 `payload.data` 为业务 JSON 字符串时，应用先解析该字符串，再选择卡片。这个 MQTT 兼容入口不负责多条消息之间的文本流重组。

这里“卡片”指上面包含 `code`、`general`、`custom` 的对象。AI 入口当前期待 `SKILL.data` 直接是卡片，并不是任意 MQTT 兼容外壳都能在 AI 入口使用。

### 暂停和继续播放

```json
{
  "bizType": "SKILL",
  "data": {
    "code": "PlayControl",
    "general": {
      "template": { "name": "audio", "version": "1.0" },
      "action": "resume",
      "data": { "preTtsFlag": true }
    }
  }
}
```

将 `action` 改为 `stop` 就是云端的暂停动作。注意 `PlayControl` 大小写，以及这里的 `stop` 并非销毁播放器。本地 `MusicPlayer::Stop()` 才会取消播放并丢弃状态；两者不能混用。

### 历史自定义卡片

```json
{
  "code": "music",
  "custom": {
    "action": "play",
    "data": {
      "preTtsFlag": false,
      "audios": [
        { "format": "mp3", "url": "https://example.com/music.mp3" }
      ]
    }
  }
}
```

当前播放器优先选择带字符串 `action` 的 `custom`；否则选择带字符串 `action` 的 `general`。选中一个容器后，从同一容器的 `data` 取 `audios` 和 `preTtsFlag`，不跨容器合并或补齐。因此 `custom={"data":{}}` 不会挡住 `general`，但带 `action` 的不完整 `custom` 会优先于 `general`。

## 字段语义和当前支持范围

`code` 当前接受 `music`、`story` 和 `PlayControl`。`story` 复用同一个音频播放器，并不代表已实现完整的故事业务。动作和资源列表决定实际播放行为，`template` 的名称与版本目前不作为准入校验。

| 动作 | 当前固件行为 | 后续实现需注意 |
| --- | --- | --- |
| `play` | 要求 `data.audios` 数组，替换旧音乐并播放有效 URL | 不是仅凭 NLG 开始播放 |
| `next`、`prev` | 同 `play`，要求卡片携带新的 `audios` | 不支持无 URL 的本地上一首或下一首导航 |
| `stop` | 暂停已有流，保留解码及待播状态 | 尚未开始的待播列表会被取消 |
| `resume` | 恢复保留的流，可以不带 `audios` | 没有可恢复的播放状态时返回失败；已在播放时作为已满足处理 |
| `replay`、`reset` | 未实现 | 不能因解析到了 `PlayControl` 就认为动作已执行 |
| `single_loop`、`sequential_loop`、`random_loop`、`no_loop` | 未实现 | 云端枚举存在这些动作，当前固件没有对应循环策略 |
| `local_play`、`cloud_play` | 未实现 | 需要另外设计播放模式切换 |
| `music_list`、`refresh_play_url` | 未实现 | 云端有基于 `general.data.items` 的实现，不是 `audios` 播放列表 |

Polysense 的 `music_list` 返回列表标识、分页信息和不含播放 URL 的资源元数据；`refresh_play_url` 返回刷新结果和 `items`。失败时也可能推送结构化错误结果。后续应按动作另写业务处理，不能把 `items` 直接当成可播放的 `audios`。

当前有效音频项必须同时满足：`format` 为小写 `mp3`，`url` 以 `http://` 或 `https://` 开头，URL 长度小于 2048 字节。仅检查数组前 16 项，过滤不支持项后按序播放；若没有有效项则拒绝卡片。`audioId`、歌名、歌手、专辑、封面和时长目前不参与播放控制，也没有实现卡片指定的自定义请求头、POST 或 `requestBody`。

## 前置 TTS 和中断位置续播

`preTtsFlag=true` 表示音乐需要等待前置 TTS。当前实现只将 JSON 布尔值 `true` 视为开启；缺失、`false`、字符串或数字都不等同于开启。即使不开启，也不能抢占已经在输出的 TTS。

`MusicStartGate` 保存当前对话轮次和 TTS 状态，避免上一轮完成事件放行下一轮卡片。卡片可以先于或晚于本轮 TTS 完成到达；本轮完成状态不会因卡片晚到就立即丢失。新列表的门控等待上限为 15 秒，超时、轮次过期或 TTS 中止时跳过该待播列表，而非强行播放。这个上限不等于 HTTP 下载超时，也不是暂停续播的失效时长。

收到最终 ASR 时，应用通知播放器开始新一轮：正在播放的音乐暂停，尚未开始的列表取消。TTS 开始时也会暂停音乐。暂停保留 HTTP 连接、MP3 解码器、重采样器和待播 PCM；后续 `resume` 可在满足 TTS 门控后从保留状态继续输出。仅 TTS 结束不会自动恢复被用户暂停的音乐，需要显式续播请求。

这是同一次运行期间保留流的续播，不是持久化播放进度或根据 MP3 字节比例重新定位。设备重启、替换歌曲、本地停止或连接失效后，不保证能够恢复。长时间暂停后的网络连接存活、URL 有效期及失败后的重建策略仍需产品化处理。

音频服务将音乐 PCM 和对话 PCM 分开排队，TTS 优先。不能让对话解码器重置、语音打断或 TTS 清理误删需要续播的音乐数据；若改动音频背压或中断代码，应重点回归这一点。

## 下载解码和资源边界

MP3 文件可能有 ID3 头，HTTP 分片边界也不等于 MP3 帧边界。当前使用 MP3 Simple Decoder 的流模式，保存未消费输入，按解码器反馈处理输出空间不足和 EOF；不能恢复为直接把每个 HTTP chunk 交给帧解码器，或在报错后逐字节丢弃输入来“找帧”。解码结果转换为单声道，必要时重采样到设备输出采样率，再进入音乐队列。

主要边界如下，后续修改需同时检查主机测试和板端内存：

- 音乐任务仅在 `CONFIG_PROTOCOL_TUYA` 下创建，使用 PSRAM 栈。
- TAI 文本重组单文档上限为 48 KiB；MQTT 应用消息上限为 64 KiB。
- MQTT 最多保留 2 条待处理应用消息，满时日志告警并丢弃，不阻塞 SDK 接收循环。这不是可靠业务重试或持久队列，也不代表总内存只用 128 KiB，JSON、TLS、音频等仍另占内存。
- HTTP 每次读取 4096 字节，编码缓冲限制 64 KiB，解码输出缓冲限制 16 KiB。HTTP 客户端超时配置为 10 秒，HTTPS 使用证书 bundle 校验。
- 当前未实现基于业务标识的去重、跨通道幂等或播放成功回执。重复 `play` 卡片可能替换并重新开始音乐；不能把收到卡片或 `accepted=1` 视为下载、解码和实际外放均成功。

## 排障和后续验收

先确认通道，再确认卡片和动作，最后确认播放器。日志中打印 NLG“即将播放”只说明云端生成了回复。

1. AI 卡片检查 `bizType=SKILL` 和 `data.code`；MQTT 检查 `MQTT application callback`，随后看 `Tuya MQTT candidate skill code` 与 `Tuya MQTT skill accepted`。原始日志的 `packet-type=text` 本身不能证明是 MQTT。
2. `Queued music playlist` 表示卡片已转换成待播 URL，不代表已开始输出。检查前置 TTS 是否完成、是否进入新轮次，以及是否出现 `TTS not ready; skipping music`。
3. `Starting music download` 和 `Music HTTP ... MP3 stream decoder ready` 表示进入下载解码阶段。HTTP 错误、解码失败或资源不足需要继续排查，不能仅根据 HTTP 200 判断播放成功。
4. `Music paused; stream retained for resume` 表示暂停；`Cannot resume: no interrupted music stream` 表示没有可恢复状态；`Music resumed after TTS; continuing retained stream` 表示续播控制已经放行。还需检查实际输出。

新增功能至少覆盖：AI 完整及分片卡片、MQTT 9000 和已解包卡片、`custom` 与 `general` 优先级、TTS 前后到达、无 URL 续播、暂停后替换曲目、重复消息、队列满、非法字段、不支持格式、HTTP 失败及长时间暂停。需要新动作时扩展共用业务层，不在两个通道各写一套播放逻辑；若新增去重，应明确业务标识、有效期及不同轮次的处理，不能把“同一 URL”简单视为重复。

现有主机回归入口可从项目根目录运行：

```sh
AUDIO_ABORT_NO_SANITIZER=1 python3 -m unittest \
  tests.test_music_stream_host tests.test_music_control_host \
  tests.test_music_pipeline_host tests.test_tuya_music_skill \
  tests.test_tuya_mqtt_skill_host tests.test_audio_abort -q
```

其中流解码主机测试用脚本化替身验证调用契约、分片、消费长度、输出扩容及 EOF，不是验证乐鑫 MP3 库真实解码质量；音频中断测试在上述命令中关闭 sanitizer。实际音质、资源兼容性和板端内存仍需实机验证。

## 代码和资料入口

设备代码均为仓库内相对路径：

- [Tuya 协议入口](../main/protocols/tuya_protocol.cc)：`OnMqttMessage`、`MqttPumpLoop`、`HandleText`、`HandleEvent`。
- [MQTT 卡片选择](../main/protocols/tuya_mqtt_skill.cc)和[投递限流](../main/protocols/tuya_mqtt_delivery_limiter.h)。
- [TAI 文本重组](../main/protocols/tuya_text_stream.cc)。
- [应用分发与 TTS 事件](../main/application.cc)：`HandleTuyaMqttMessage` 和 `OnIncomingJson` 注册处。
- [音乐播放器](../main/audio/music_player.cc)、[播放状态](../main/audio/music_playback_state.h)和[TTS 门控](../main/audio/music_start_gate.h)。
- [音频队列与输出](../main/audio/audio_service.cc)：`HandleTuyaMusicSkill`、音乐 PCM 队列及输出仲裁。
- [MQTT 卡片测试](../tests/tuya_mqtt_skill_test.cc)、[控制测试](../tests/test_music_control_host.py)和[流解码契约测试](../tests/test_music_stream_host.py)。

云端实现需在 Polysense 仓库查看，不是本项目内的依赖。以下路径均相对于其 `polysense-service/src/main/java/com/tuya/polysense/service/` 目录：

- `music/impl/intent/PlayMusicService.java`：`handleIntent` 与 `doPushMQTT` 的通道选择。
- `music/impl/MusicIntentService.java`：`createAudioInfoResp` 的音频字段；`music/impl/MusicToolService.java`：历史工具路径。
- `playcontrol/enums/PlayControlIntentEnum.java`：动作及 TCP、MQTT 能力标记。枚举描述云端能力，不等于当前固件已支持。
- `playcontrol/impl/PlayControlIntentService.java`、`playcontrol/impl/intent/PlayAudioService.java`、`playcontrol/impl/intent/StopAudioService.java`：播控卡片及前置 TTS。
- `kafka/handle/PlayControlSkillHandle.java`：MQTT 请求处理及 9000 下行构造。
- `playcontrol/impl/MusicListMqttService.java`、`playcontrol/impl/RefreshPlayUrlMqttService.java`：列表查询及播放地址刷新扩展。

TuyaOpen 的 `src/ai_components/ai_skills/src/ai_skill.c` 按 `music`、`story`、`PlayControl` 分发；`skill_music_story.c` 解析 `custom`、`general` 并实现播控动作，可参考业务语义，但不能据此声称本项目拥有相同功能。

[Agentic-kit 官方音乐教程](https://agentic-kit.tuya.com/docs/tutorials/music-play/)说明通过 `on_text` 返回 SKILL 并提取资源 URL 的示例。它是入门参考，不覆盖本项目的双通道接入、TTS 输出仲裁和保留流续播。
