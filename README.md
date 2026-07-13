# Xiaozhi 涂鸦版

## Tuya版介绍

此项目是从 https://github.com/78/xiaozhi-esp32 fork 过来, 然后使用的是 Tuya 的 AI 服务. 相比小智原版, 此版本主要改动功能:

* **配网功能** — 需使用 Tuya App（或者 Tuya 的 OEM app 或基于 App SDK 开发的 app）配网, 使用的是蓝牙配网, 配网速度快, 使用比较方便
* **OTA 功能** — 使用 Tuya 云提供的 OTA, 如果要升级, 需要先在 Tuya IoT 平台新建 OTA 固件, 并上传和发布
* **AI 功能** — 基本和小智的功能差不多, Tuya 的云端可以做更多定制, 可以改工作流, 可以生图或做图片理解等

底层 AI 通信使用 [agentic-kit](https://github.com/tuya/agentic-kit) 的 ESP-IDF 组件（`esp-agentic-kit`），通过涂鸦 tRTC 实时通道与云端 AI 交互。更多 SDK 文档请参考 [agentic-kit 文档站](https://agentic-kit.tuya.com/docs/intro)。

---

## 前提条件

### 1. 环境准备

* **ESP-IDF >= 5.5** — 安装及配置方法参考 [ESP-IDF 编程指南](https://docs.espressif.com/projects/esp-idf/zh_CN/latest/esp32s3/get-started/)
* 确认 `idf.py` 可正常使用

### 2. Tuya IoT 平台准备

使用此版本前, 需要在 [Tuya IoT 平台](https://iot.tuya.com) 完成以下准备:

| 前提条件 | 说明 | 获取方式 |
|----------|------|----------|
| **产品 PID** | 标识一类设备的共同配置（功能点、面板、绑定的 AI Agent） | 在 IoT 平台创建产品后获得，详见[创建和配置 Agent](https://agentic-kit.tuya.com/docs/guides/create-agent) |
| **设备授权码**（uuid / authkey） | 每台设备独立持有, 用于激活并换取云端凭据 | 在 IoT 平台领取免费测试授权码, 详见[领取授权码](https://agentic-kit.tuya.com/docs/get-authkey) |

大致流程:

1. 在 Tuya IoT 平台创建产品, 获得 **产品 PID**
2. 为产品配置 **AI Agent**（系统提示词、TTS 语音类型、语言设置等）
3. 领取测试用 **授权码**（uuid + authkey）
4. （可选）配置工作流实现图片理解、结构化输出等高级功能

> 💡 测试阶段可在 IoT 平台免费领取 2 个授权码; 大规模出货需联系 Tuya 商务购买。

---

## 配置和编译

### 1. 配置授权码信息

在项目根目录创建 `tuya_authkey.txt` 文件, 填入你的产品 PID 和授权码:

```
TUYA_UUID=your_uuid_here
TUYA_AUTH_KEY=your_authkey_here
TUYA_PRODUCT_KEY=your_product_pid_here
```

构建系统会自动读取此文件并生成 `tuya_authkey.h` 头文件, 供 BLE 配网和设备激活使用。

> ⚠️ 此文件包含设备凭据, 请勿提交到公开仓库。建议将其加入 `.gitignore`。

### 2. menuconfig 配置

```sh
idf.py menuconfig
```

关键配置项位于 **Xiaozhi Assistant** 菜单:

| 配置项 | 说明 |
|--------|------|
| `PROTOCOL_TUYA` | 启用 Tuya AI 2.1 协议（默认已在 `sdkconfig.defaults` 中开启） |
| `TUYA_BLE_PROVISIONING` | 使用 Tuya BLE 蓝牙配网（默认开启, 依赖 `PROTOCOL_TUYA`） |
| `Board Type` | 选择你的开发板型号 |
| `Default Language` | 选择设备显示语言 |
| `Wake Word Implementation Type` | 选择唤醒词方案（需 PSRAM 支持） |

> `sdkconfig.defaults` 已默认启用 `CONFIG_PROTOCOL_TUYA=y` 和 `CONFIG_TUYA_BLE_PROVISIONING=y`。

### 3. 编译和烧录

```sh
idf.py set-target esp32s3    # 根据你的开发板选择目标芯片
idf.py build
idf.py flash monitor
```

---

## 配网使用

### BLE 蓝牙配网

设备首次启动时, 会自动进入 BLE 配网模式（等待 60 秒）:

1. 在手机上打开 **Tuya App**（或 OEM app / 基于 App SDK 开发的 app）
2. 点击添加设备, App 会搜索到附近的设备
3. 选择设备后, App 通过 BLE 将 WiFi 凭据和配网 Token 传递给设备
4. 设备收到后自动连接 WiFi 并完成云端激活
5. 激活成功后, 凭据（`devid`、`secret_key`、`local_key`）会保存到 NVS, 后续启动无需重复配网

> 如果 BLE 配网超时（60 秒内未完成）, 设备会回退到 SoftAP 热点配网模式。

配网使用的 App 可以是以下任意一种:
* Tuya App（或 Smart Life App）
* 基于 Tuya App 进行 OEM 的 App（零开发）
* 基于 Tuya App SDK 开发的 App（有差异化需求）

---

## OTA 固件升级

此版本使用涂鸦云提供的 OTA 升级, 流程如下:

1. **上报版本** — 设备启动时自动向云端上报当前固件版本
2. **检查升级** — 设备向云端查询是否有待升级固件
3. **下载烧写** — 如有升级, 设备下载固件并写入 OTA 分区
4. **重启生效** — 烧写完成后切换启动分区并重启

### 发布新固件

要在 Tuya IoT 平台发布 OTA 固件:

1. 编译固件: `idf.py build`
2. 在 Tuya IoT 平台进入 **产品开发** → **设备升级** → **新建 OTA 固件**
3. 上传编译生成的固件文件（`build/xiaozhi-esp32.bin`）
4. 填写版本号并发布

设备下次启动检查到新版本后会自动下载升级。

> 📌 OTA 需要双 app 分区（`ota_0` / `ota_1`）, 项目默认分区表已配置好。

---

## AI 功能

此版本通过涂鸦 AI 云平台提供 AI 能力, 基础功能与小智原版一致, 同时支持更多云端定制:

* **语音对话** — 实时语音交互, 支持 ASR + LLM + TTS
* **图片理解** — 通过配置工作流, 设备可发送图片给云端做识别和理解
* **图片生成** — 通过工作流配置实现以文生图
* **设备 MCP** — AI 可调用设备侧工具（读传感器、控制外设等）
* **云侧 MCP** — 支持天气查询、联网搜索等第三方能力扩展
* **工作流定制** — 在 Tuya 平台配置工作流, 修改后立即生效, 无需重新连接

### Agent 配置

AI Agent 的行为在 Tuya IoT 平台配置:
- 系统提示词（System Prompt）
- TTS 语音类型
- 语言设置
- 工作流（用于图片理解、结构化输出等高级场景）

更多详情参考:
- [创建和配置 Agent](https://agentic-kit.tuya.com/docs/guides/create-agent)
- [创建工作流](https://agentic-kit.tuya.com/docs/guides/create-workflow)
- [设备 MCP 指南](https://agentic-kit.tuya.com/docs/guides/device-mcp)

---

## 相关文档

- [Agentic-kit 介绍](https://agentic-kit.tuya.com/docs/intro/)
- [核心概念](https://agentic-kit.tuya.com/docs/concepts) — 设备激活、配网、AI Agent、tRTC、数据点等
- [BLE 蓝牙配网教程](https://agentic-kit.tuya.com/docs/tutorials/pair-by-ble)
- [固件 OTA 升级指南](https://agentic-kit.tuya.com/docs/guides/ota-upgrade)
- [Tuya IoT 平台](https://iot.tuya.com)
- [原版小智项目](https://github.com/78/xiaozhi-esp32)
