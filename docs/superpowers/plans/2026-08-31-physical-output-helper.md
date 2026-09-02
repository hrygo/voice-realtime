# Physical Output Helper Implementation Plan

> **For agentic workers:** Implement task-by-task with TDD. Keep the Swift Helper, Python adapter, and packaging commits independently reviewable.

**Goal:** 在不接入字幕/会议产品路径的前提下，交付可签名的 macOS 物理输出采集 Helper、严格 UDS v1 协议、Python `PhysicalOutputSource` 和组件级故障恢复，为 P2 output-only 字幕提供已验证输入源。

**Architecture:** Swift Helper 作为唯一 Core Audio/TCC 边界，创建绑定目标 `deviceUID` 的私有 Tap 与 Aggregate Device；实时回调仅复制到预分配 SPSC Ring，工作队列完成 16 kHz mono s16le 归一化并经用户私有 UDS 发送。Python 负责 Helper 进程、协议校验、请求关联、有界 PCM 队列和 `AudioSource` 生命周期。P1 不修改 `RuntimeModeCoordinator`、字幕启动命令、会议数据库或页面来源选择。

**Tech Stack:** Swift 6.3 / SwiftPM、CoreAudio、AVFAudio、Foundation、POSIX UDS、Python 3.12 asyncio/Pydantic、pytest。

**Spec:** `docs/superpowers/specs/2026-08-31-physical-output-audio-capture-design.md`

**Local toolchain fact (2026-08-31):** 本机仅安装 Apple Command Line Tools，无完整 Xcode；可执行 `swift build`、手工组装 `.app` 与 `codesign`，但当前 CLT 不包含 `Testing`/`XCTest` 运行库，`swift test` 无法形成有效门禁。因此本包使用零外部依赖的 `swift run sona-audio-capture-selftest` 执行同等断言；标准 XCTest、Developer ID 签名、公证、Xcode 工程归档及完整设备权限矩阵必须作为独立人工门禁，不能由本计划伪造完成。

## Implementation Status (2026-08-31)

**Status:** `P1 code complete`；`P1 device gate pending`；产品仍保持 mic-only。

- Python：1408 项全量测试通过，覆盖率 82.69%；mypy 115 个 source files、ruff 全绿。
- 前端：35 个 test files / 222 项测试通过，生产构建通过；本阶段没有页面或交互变化。
- Swift：普通、TSan、ASan 各 52 项自测通过，warnings-as-errors 与 Release 构建通过。
- Bundle：ad-hoc + Hardened Runtime 静态门禁通过；安全枚举到 1 个输出设备、1 个默认设备。
- 隐私：未发现真实 token、设备 UID 日志、网络监听或受版本控制的 PCM/WAV/Socket 制品。
- 未验证：真实 capture/TCC、内建/有线/蓝牙/USB/HDMI、默认输出切换、2 小时长稳、Developer ID、公证、Xcode Archive/XCTest。

实现提交：`3bb6d94`、`0d8c25b`、`1bc9047`、`5d2a510`、`7c3ee54`、`fada9de`、
`ba6dfae`、`7e3d433`、`af7af44`、`8c66c6f`、`f48ec36`、`f3a1f7a`。

## Stable IPC v1 Contract

所有多字节整数使用 big-endian。公共前缀固定 16 字节：

| offset | type | field |
|---:|---|---|
| 0 | `u32` | magic `0x56524143` (`VRAC`) |
| 4 | `u16` | `header_length` |
| 6 | `u8` | `protocol_major=1` |
| 7 | `u8` | `protocol_minor=0` |
| 8 | `u8` | `message_type`：JSON=1、PCM=2 |
| 9 | `u8` | prefix flags，v1 必须为 0 |
| 10 | `u16` | reserved，v1 必须为 0 |
| 12 | `u32` | `body_length` |

JSON 帧的 `header_length=16`，UTF-8 body 最大 65,536 bytes。PCM 帧的 `header_length=84`，额外头依次为：`capture_uuid[16]`、`source_uuid[16]`、`device_generation:u32`、`sequence:u64`、`host_time_ns:u64`、`sample_rate:u32`、`samples_per_channel:u16`、`channels:u8`、`sample_width:u8`、`frame_flags:u32`、`payload_length:u32`。v1 PCM 必须为 16,000 Hz、mono、s16le、512 samples、1,024-byte payload。

控制消息使用小写 snake_case `type` 与不超过 64 字符的 `request_id`。错误统一为 `error {code,message,retryable,request_id}`；不得返回 Swift 堆栈、路径、设备 UID、token 或 PCM。未知 major 拒绝连接；同 major 的未知 minor 只允许按 `header_length` 跳过扩展头，未知 message type 拒绝。

## Global Constraints

- Helper 最低 macOS 14.2；低版本返回稳定 `unsupported_os`。
- 不新增 Python/npm 依赖；Swift 仅使用系统 framework，不拉取外部 package。
- Socket 父目录 `0700`、socket `0600`；Helper 使用 `getpeereid` 校验有效 UID，并校验 256-bit capture token。
- IPC、日志、测试 fixture、数据库和运行目录不得持久化真实 PCM；协议测试只使用合成常量样本。
- Core Audio 回调中禁止日志、分配、锁等待、格式转换、Socket/File I/O 和状态迁移。
- 设备 UID 只存在 Helper 内存与固定用户私有配置边界；协议只暴露由本机 0600 install key 派生的稳定 opaque `device_ref`。
- 任何 device-scoped Tap 构造失败都 fail closed 为 `unsupported_device_scope`，不切换到全局 Tap。
- P1 不解锁 `physical_output`/`dual` 产品命令；P2 前不得宣称会议或字幕已可使用物理输出。

---

## Task 1: 固化跨语言 UDS v1 契约与 Python codec

**Files:**
- Create: `contracts/audio-capture/v1/README.md`
- Create: `contracts/audio-capture/v1/control-message.schema.json`
- Create: `contracts/audio-capture/v1/fixtures/hello.json`
- Create: `contracts/audio-capture/v1/fixtures/pcm-header.hex`
- Create: `src/sona/audio/ipc.py`
- Create: `tests/test_audio_capture_ipc.py`

- [x] **Step 1: 先写 Python 失败测试**

覆盖：JSON/PCM golden fixture、分段读取、错误 magic/major/type、超长 JSON、payload 长度不一致、非标准 PCM、未知 minor 扩展头跳过、错误结构脱敏。

```python
def test_pcm_header_fixture_round_trips_with_synthetic_silence() -> None:
    message = decode_wire_message(bytes.fromhex(PCM_HEADER_FIXTURE) + bytes(1_024))
    assert isinstance(message, PCMMessage)
    assert message.sample_rate == 16_000
    assert len(message.pcm) == 1_024

@pytest.mark.parametrize("field", ["magic", "major", "message_type", "body_length"])
def test_decoder_rejects_invalid_boundary(field: str) -> None:
    ...
```

- [x] **Step 2: 运行红灯**

Run: `uv run pytest tests/test_audio_capture_ipc.py -q --no-cov`

Expected: FAIL，提示 `sona.audio.ipc` 不存在。

- [x] **Step 3: 实现不可变 wire 类型和增量 parser**

`ipc.py` 只负责 bytes ↔ typed message；不打开 socket、不启动进程。对每个外部字段在边界一次性严格校验，内部代码不重复防御。

- [x] **Step 4: 运行测试、mypy、ruff 并提交**

Run: `uv run pytest tests/test_audio_capture_ipc.py -q --no-cov`

Run: `uv run mypy src/sona/audio/ipc.py`

Run: `uv run ruff check src/sona/audio/ipc.py tests/test_audio_capture_ipc.py`

Commit: `feat(audio): 固化物理输出采集 IPC v1 契约`

---

## Task 2: Python Helper client、supervisor 与 `PhysicalOutputSource`

**Files:**
- Create: `src/sona/audio/output_source.py`
- Modify: `src/sona/audio/__init__.py`
- Modify: `src/sona/config.py`
- Create: `tests/test_output_source.py`
- Modify: `tests/test_config.py`

- [x] **Step 1: 写 fake UDS server 行为测试**

覆盖：socket 所有者/权限、hello token、request_id 关联、prepare/commit、PCM 转换、drop-oldest、错误码映射、断线进入 `failed`、stop/abort 幂等、子进程启动超时与有界退避。测试只启动临时 UDS，不启动 Core Audio。

- [x] **Step 2: 运行红灯**

Run: `uv run pytest tests/test_output_source.py tests/test_config.py -q --no-cov`

Expected: FAIL，提示输出来源和配置尚不存在。

- [x] **Step 3: 增加 `AudioCaptureSettings`**

默认 `enabled=false`。字段包含 helper executable、本机 runtime dir、启动/命令超时、有界队列、最大重启次数与退避；配置 dump 不输出 token 或原始 UID。

- [x] **Step 4: 实现 client/supervisor/source**

`AudioCaptureClient` 独占 reader task；控制响应进入按 `request_id` 建立的 future，PCM 进入有界队列。`HelperSupervisor` 只执行固定 executable，不接受协议传入命令或路径。`PhysicalOutputSource` 实现既有 `AudioSource`，source ID 使用 Helper 返回的会话 UUID。

- [x] **Step 5: 运行聚焦门禁并提交**

Run: `uv run pytest tests/test_audio_capture_ipc.py tests/test_output_source.py tests/test_config.py -q --no-cov`

Run: `uv run mypy src/sona/audio/ src/sona/config.py`

Run: `uv run ruff check src/sona/audio/ src/sona/config.py tests/test_output_source.py tests/test_config.py`

Commit: `feat(audio): 增加物理输出 Helper 客户端与来源适配器`

---

## Task 3: Swift Package、协议 codec 与预分配 SPSC Ring

**Files:**
- Create: `native/sona-audio-capture/Package.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureRing/include/SonaAudioCaptureRing.h`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureRing/SonaAudioCaptureRing.c`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/WireProtocol.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/PCMFrame.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureHelper/main.swift`
- Create: `native/sona-audio-capture/Tests/SonaAudioCaptureCoreTests/WireProtocolTests.swift`
- Create: `native/sona-audio-capture/Tests/SonaAudioCaptureCoreTests/RingBufferTests.swift`
- Modify: `.gitignore`

- [x] **Step 1: 写 Swift golden fixture 与 ring 失败测试**

Swift 必须读取与 Python 相同的 hex fixture；ring 测试覆盖固定容量、drop-oldest、sequence gap、clear 不残留内容和超限拒绝。

- [x] **Step 2: 运行红灯**

Run: `cd native/sona-audio-capture && swift run sona-audio-capture-selftest`

Expected: FAIL，目标/类型尚不存在。

- [x] **Step 3: 实现 codec 与 C11 atomic SPSC Ring**

Ring 初始化时一次性分配固定 slot；push/pop 只执行原子索引、边界检查和 `memcpy`。Swift wrapper 不在 callback 路径创建 `Data`。

- [x] **Step 4: 运行 sanitizer 可用范围内的测试并提交**

Run: `cd native/sona-audio-capture && swift run sona-audio-capture-selftest`

Run: `cd native/sona-audio-capture && swift build -c release`

Commit: `feat(native): 建立采集协议与无锁音频环形缓冲`

---

## Task 4: 输出设备目录与严格 device scope

**Files:**
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/CoreAudioProperty.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/DeviceCatalog.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/DeviceReference.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/DeviceReferenceStore.swift`
- Create: `native/sona-audio-capture/Tests/SonaAudioCaptureCoreTests/DeviceCatalogTests.swift`

- [x] **Step 1: 写纯模型与 HAL adapter 测试**

覆盖：只列 alive 且有输出 channel 的设备、默认标记、名称清洗、transport 分类、install key 派生稳定 opaque `device_ref`、未知 ref 拒绝、默认设备为空时稳定错误。

- [x] **Step 2: 实现可注入 HAL property reader**

生产 adapter 使用 `AudioObjectGetPropertyData*`；测试 adapter 不访问真实设备。install key 固定写入 Helper 的 Application Support 子目录并强制 `0600`，不得由 IPC 指定路径。禁止把 UID 放进 `description`、`CustomStringConvertible` 或错误文本。

- [x] **Step 3: 运行 Swift 测试与真实只读枚举冒烟**

Run: `cd native/sona-audio-capture && swift run sona-audio-capture-selftest`

Run: `cd native/sona-audio-capture && swift run sona-audio-capture-helper --list-devices-json`

Expected: 冒烟只输出清洗标签、类别、default 与 opaque ref；不触发系统音频权限。

Commit: `feat(native): 增加输出设备枚举与私密引用`

---

## Task 5: Core Audio Tap、Aggregate Device 与 PCM 归一化

**Files:**
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/TapCaptureEngine.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/CoreAudioHAL.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/AudioNormalizer.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/FrameAccumulator.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/HostClock.swift`
- Create: `native/sona-audio-capture/Tests/SonaAudioCaptureCoreTests/AudioNormalizerTests.swift`
- Create: `native/sona-audio-capture/Tests/SonaAudioCaptureCoreTests/FrameAccumulatorTests.swift`
- Create: `native/sona-audio-capture/Tests/SonaAudioCaptureCoreTests/TapLifecycleTests.swift`

- [x] **Step 1: 写 converter、32 ms 累积和逆序清理测试**

使用合成 Float32 mono/stereo 数据验证 48/44.1 kHz → 16 kHz mono int16、限幅、512-sample 分帧、host time 递增、generation/discontinuity、prepare 失败时 Tap/Aggregate/I/O 逆序回滚。

- [x] **Step 2: 实现 `TapCaptureEngine`**

使用 `CATapDescription(excludingProcesses:deviceUID:stream:)`，设置 unmuted/private/mixdown；通过 `AudioHardwareCreateProcessTap` 和私有 Aggregate Device 创建输入。I/O callback 只读取 timestamp 并推入 C ring，工作队列执行 `AVAudioConverter`、分帧和输出闭包。

- [x] **Step 3: 增加 PID 排除和稳定错误映射**

通过 `kAudioHardwarePropertyTranslatePIDToProcessObject` 转换显式排除 PID；不存在 PID 忽略，device scope、权限、HAL、format 与 callback timeout 映射为固定 code，不暴露 `OSStatus` 之外的内部信息。

- [x] **Step 4: 编译与测试并提交**

Run: `cd native/sona-audio-capture && swift run sona-audio-capture-selftest`

Run: `cd native/sona-audio-capture && swift build -c release`

Commit: `feat(native): 实现设备绑定 Core Audio Tap 采集引擎`

---

## Task 6: UDS server 与两阶段 Helper 状态机

**Files:**
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/UnixPeer.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/CaptureServer.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/CaptureController.swift`
- Create: `native/sona-audio-capture/Sources/SonaAudioCaptureHelper/main.swift`
- Create: `native/sona-audio-capture/Tests/SonaAudioCaptureCoreTests/CaptureControllerTests.swift`
- Create: `native/sona-audio-capture/Tests/SonaAudioCaptureCoreTests/CaptureServerTests.swift`

- [x] **Step 1: 写权限、token、状态机和背压失败测试**

覆盖：非同 UID、错误 token、第二客户端、commit-before-ready、capture ID 不匹配、重复 stop、JSON 上限、慢客户端 drop-oldest、断线立即停止 Tap、错误响应脱敏。

- [x] **Step 2: 实现单客户端 UDS server**

只绑定 Python 提供的私有目录内 socket；拒绝 symlink/非 owner 目录，bind 后 chmod `0600`。写队列固定上限并由专用 writer 发送，Core Audio 工作队列不阻塞 Socket。

- [x] **Step 3: 实现 prepare/commit/abort/stop**

prepare 创建并启动 Tap 但丢弃业务 PCM，首个有效 callback 后才返回 ready；commit 原子开启发送；abort/stop 清零 ring、停止 I/O 并逆序释放；连接关闭走 stop。

- [x] **Step 4: Swift 全测、Python fake-server 互操作测试并提交**

Run: `cd native/sona-audio-capture && swift run sona-audio-capture-selftest`

Run: `uv run pytest tests/test_audio_capture_ipc.py tests/test_output_source.py -q --no-cov`

Commit: `feat(native): 完成采集 Helper 两阶段 UDS 服务`

---

## Task 7: `.app` 打包、签名与本机运行脚本

**Files:**
- Create: `native/sona-audio-capture/Resources/Info.plist`
- Create: `native/sona-audio-capture/Resources/SonaAudioCapture.entitlements`
- Create: `scripts/build-audio-capture-helper.sh`
- Create: `scripts/test-audio-capture-helper.sh`
- Modify: `README.md`
- Modify: `docs/README.md`

- [x] **Step 1: 写静态 bundle 契约测试脚本**

验证 Bundle ID、`LSUIElement`、macOS 14.2、`NSAudioCaptureUsageDescription`、Hardened Runtime、Mach-O 架构、无网络 entitlement、socket/PCM 无 bundle 资源。

- [x] **Step 2: 实现可移植构建脚本**

脚本调用 `swift build -c release`，组装 `build/sona-audio-capture/sona-audio-capture.app`。开发默认 ad-hoc 签名并明确标记“不可发布”；提供 `SONA_AUDIO_CAPTURE_SIGNING_IDENTITY` 和发布 timestamp 参数，但不在仓库记录证书名。

- [x] **Step 3: 构建、签名校验与无权限枚举冒烟**

Run: `scripts/build-audio-capture-helper.sh`

Run: `codesign --verify --deep --strict --verbose=2 build/sona-audio-capture/sona-audio-capture.app`

Run: `scripts/test-audio-capture-helper.sh --list-devices`

- [x] **Step 4: 更新运行文档并提交**

文档明确：首次真实 capture 会触发系统“系统音频录制”授权；无完整 Xcode 时未执行 Developer ID/公证；禁止把 ad-hoc 构建描述为发布制品。

Commit: `build(native): 增加采集 Helper 应用打包与签名校验`

---

## Task 8: P1 自动化门禁与人工设备验收入口

**Files:**
- Create: `tests/test_audio_capture_bundle.py`
- Create: `scripts/smoke_audio_capture_helper.py`
- Create: `docs/manuals/物理输出音频采集验收手册.md`
- Modify: `src/sona/config.py`
- Modify: `tests/test_output_source.py`
- Modify: `native/sona-audio-capture/Sources/SonaAudioCaptureCore/CaptureServer.swift`
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/superpowers/plans/2026-08-31-physical-output-helper.md`

- [x] **Step 1: 运行完整自动化门禁**

Run: `SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/`

Run: `uv run mypy src/`

Run: `uv run ruff check src/ tests/`

Run: `cd ui && npm test -- --run`

Run: `cd ui && npm run build`

Run: `cd native/sona-audio-capture && swift run sona-audio-capture-selftest`

Run: `scripts/build-audio-capture-helper.sh && scripts/test-audio-capture-helper.sh --static`

- [x] **Step 2: 验证隐私与构建产物边界**

确认 source/test/docs 中没有真实设备 UID、token、签名身份、PCM 文件或网络监听；运行目录清理后不留 Tap、Aggregate Device、socket 或音频制品。

- [x] **Step 3: 提供显式人工 capture 冒烟**

手册提供用户主动执行的内建输出设备 30 秒测试：授权、播放合成测试音、观察非零 level/sequence、停止、确认无 PCM 文件。该步骤不得在自动测试中自行触发 TCC 弹窗。

- [x] **Step 4: 记录本机实际门禁状态**

自动化通过可标记 `P1 code complete`；只有内建、有线、蓝牙、USB、HDMI 与默认设备切换/2h 长稳全部通过，才标记规格中的 P1 device gate complete。当前缺少的设备或完整 Xcode/公证必须列为未验证，不得弱化为成功。

- [x] **Step 5: 自审、coverage check 与提交**

检查 Swift/Python 生命周期、parser、安全、实时性和影响面；对所有改动路径执行索引覆盖核验。更新本计划 checkbox 和状态。

Commit: `docs(audio): 记录物理输出 Helper 验收状态`

---

## P1 Completion Boundary

完成本计划的自动化部分后，仓库具备真实 Core Audio 采集组件和可测试 Python 来源，但产品仍保持 mic-only：

- 不增加页面来源选择；
- 不修改会议 v1 数据库约束；
- 不将 output source 接入 SubtitleProxy；
- 不实现 DualSourceMixer；
- 不自动触发系统权限弹窗。

P2 只在 `P1 code complete` 且至少内建输出设备人工 capture 通过后开始；P1 全设备矩阵和 2h 门禁未完成时，规格继续保持 `under_review`。
