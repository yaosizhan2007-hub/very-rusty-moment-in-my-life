# 人生中非常悲壮的瞬间

> **你找到了这个项目，在你人生中非常Rusty的瞬间**

> 当麦克风里冒出「兄弟，我尽力了」，游戏声音被强行掐掉，你自己的那首歌响彻直播间。

「人生中非常悲壮的瞬间」是一个 Windows 桌面娱乐工具（程序内部名 **Rusty Moment**），用于游戏直播、录屏与剪辑：在（手动或自动识别的）「悲壮瞬间」，通过 OBS WebSocket 切断或压低游戏音源，播放你自己准备的 BGM，放完再**恢复原始音频状态**。整个过程的重点是可靠、可控、可复现，而不是真的做情绪识别。

**本项目不包含任何音乐文件，也不会自动下载音乐。** 请使用你自己合法获得的音频。项目与任何音乐版权方、动画制作方、发行方都没有官方合作关系，示例里提到的曲名只是说明「你可以放什么」。

---

## 1. 当前实现状态（请先看这里）

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 阶段 1 | 主界面、本地音乐播放、手动触发/停止、OBS WebSocket 连接、游戏音源静音与恢复、配置保存、日志 | ✅ 已完成并通过测试 |
| 阶段 2 | 麦克风采集、VAD 切句、本地 faster-whisper 识别、关键词层、声学规则层、冷却与去重、候选/触发状态流转 | ✅ 已完成并通过真机验证 |
| 阶段 3 | AI 语义分析后端（OpenAI 兼容 + 本地 Ollama）、结构化评分校验、两种模式的概率差异、超时回退 | ✅ 已完成并通过真机验证（本地 qwen3:8b） |
| 阶段 4 | 断线重连、配置迁移 v2、性能记录、全局热键（可选）、安装/卸载文档、发行版验证 | ✅ 已完成（Windows 10 真机与「无 Python 干净机器」仍未验证，清单见 `docs/INSTALL.md`） |

**这个版本能做什么**：

- **曲目管理**：首次启动自动扫描系统「音乐」目录收三首，主界面下拉切换当前曲目，随时「添加音乐…」加入自己的文件（WAV/MP3/FLAC/OGG/AAC/M4A，AAC 由 PyAV 解码）。切曲后会自动后台预解码，下一次触发几乎零等待。
- **两种输出方式**（主界面右侧「输出方式」）：
  - **OBS 控制**：触发时静音/压低 OBS 里的游戏音源（默认）；
  - **仅本地播歌**：完全不连 OBS、不碰任何游戏/系统音频，只监听麦克风 + 放歌，平时拿它跟人开玩笑就行。
- 手动按 F8 或点按钮，OBS 里指定的游戏音源立刻静音（或压低），当前曲目开始播放，音乐结束或你点停止后，音源回到触发前的静音状态与音量。
- 打开「开始监听麦克风」后，程序用本地 faster-whisper 把你说的话转成文字，命中关键词（如「别管我」「我尽力了」）并结合声学特征（说话够长、句内长停顿、越说越小声、突然拔高、沉默收尾）算分，达到阈值就自动触发；同一句话在去重窗口内只算一次，冷却期内不重复触发。
- **AI 语义层（可选）**：可以接本地 Ollama 或任意 OpenAI 兼容 API，让模型给「悲壮/上下文/喜剧潜力」打分；超时或返回非法数据自动退回关键词+规则。默认关闭，不配也能用。
- **快捷键**：F8/F9/F10 默认只在窗口内生效；需要"游戏里也能按"可在设置里打开全局热键（会独占按键，默认关闭）。
- 命令行 `--check` 自检环境、`--perf` 测性能（结果写日志目录）。

**这个版本不能做什么**：不会控制 OBS 以外的系统音频；AI 需要你自己准备本地模型或云端 API Key（默认关闭）。

---

## 2. 技术架构与关键依赖

### 2.1 实测依赖（Windows 11 25H2 / Python 3.13.13）

| 依赖 | 版本 | 用途 | 备注 |
| --- | --- | --- | --- |
| Python | 3.13.13（实测） | 运行时 | 3.11/3.12 同样满足语法要求，但未在本机实测 |
| PySide6 | 6.12.0 | GUI | 官方 wheel，无需额外编译 |
| sounddevice | 0.5.6 | 音频播放（PortAudio） | 自带 PortAudio DLL |
| soundfile | 0.14.0 + libsndfile 1.2.2 | 解码 WAV/MP3/FLAC/OGG | MP3 支持来自 libsndfile 1.2+，已实测 |
| PyAV | 19.0.2（随 faster-whisper 安装） | 解码 AAC/M4A/WMA 等 | 自带 FFmpeg，无需另装 ffmpeg.exe |
| faster-whisper | 1.2.1 | 本地语音识别（阶段 2） | 依赖 ctranslate2 4.8.3 + onnxruntime 1.31，CPU int8 实测可用 |
| httpx | 0.28.1 | AI 语义层 HTTP 客户端 | 支持注入 MockTransport 做单测 |
| numpy | 2.5.4 | 音频缓冲 | |
| websocket-client | 1.9.2 | OBS WebSocket 传输层 | 协议解析自行实现 |
| pywin32 | 311 | DPAPI 加密存储 OBS 密码 | 缺失时降级为「每次手动输入密码」 |
| pytest / pytest-qt | 9.1.1 / 4.x | 自动化测试 | |
| PyInstaller | 6.x | 打包 | onedir 已验证 |

**为什么自己实现 OBS 客户端**：需要精确控制请求-响应超时、断线回调、可注入传输层（测试时用内存假服务器复现真实协议），几百行代码换来完整可测性。

**为什么用 OBS 内音源而不是系统级音频**：见第 13 节「已知限制」，Windows 上对任意游戏做无损、无副作用的单应用静音并不可靠，本版本不假装能做到。

### 2.2 三种音频的关系

```
麦克风输入 ──► VAD 切句 ──► 本地 faster-whisper ──► 关键词层 ┐
                     └──► 声学特征 ──► 规则层 ────────────┤
                                                          ├─► TriggerEngine
                                            （阶段 3）AI 层 ┘        │
                                                                    ▼
游戏音频（OBS 音源） ◄────────────── 静音 / 压低 ─────────── 触发决策
                                                                    │
本地 BGM 文件 ──► AudioPlayer（sounddevice）──► 播放 ──► 结束后恢复音频快照
```

### 2.3 模块职责

| 模块 | 职责 |
| --- | --- |
| `rusty_moment/config/config_manager.py` | 配置加载、类型/范围校验、原子保存、版本迁移、损坏备份 |
| `rusty_moment/security/secret_store.py` | DPAPI 加密保存 OBS 密码 / 未来 API Key |
| `rusty_moment/obs/obs_client.py` | obs-websocket 5.x 协议：Hello/Identify/Request/RequestResponse/Event |
| `rusty_moment/obs/obs_controller.py` | 音源枚举、存在性校验、读/写静音与音量、静音/压低策略 |
| `rusty_moment/audio/audio_player.py` | sounddevice 播放：播放/暂停/停止/进度/音量/设备/采样率回退 |
| `rusty_moment/audio/decoder.py` | 双后端解码：libsndfile 优先，AAC/M4A 等交给 PyAV |
| `rusty_moment/audio/music_library.py` | 扫描系统「音乐」目录、提炼标题、生成默认三首 |
| `rusty_moment/audio/audio_state_manager.py` | 事件前快照、临时控制、安全恢复、用户干预检测、异常退出留档 |
| `rusty_moment/audio/audio_router.py` | `SystemAudioBackend` 接口（未实现，仅声明限制） |
| `rusty_moment/detection/microphone_capture.py` | 麦克风设备枚举、流式采集（回调只入队）、可靠释放 |
| `rusty_moment/detection/vad.py` | 帧能量 + 自适应噪声底 + 启动学习期，切句并算声学特征 |
| `rusty_moment/detection/speech_recognizer.py` | 可替换 STT 接口 + 本地 faster-whisper（含 initial_prompt 简体引导） |
| `rusty_moment/detection/text_normalize.py` | 繁转简（Windows `LCMapStringW`，带内置兜底表）+ 标点归一 |
| `rusty_moment/detection/keyword_detector.py` | 正向词/排除词/权重/最小命中数 |
| `rusty_moment/detection/rule_analyzer.py` | 5 个声学分量的加权评分 |
| `rusty_moment/detection/emotion_analyzer.py` | AI 分析接口 + 结构化结果校验（阶段 3 接后端） |
| `rusty_moment/detection/trigger_engine.py` | 三层归一化合成、两种模式的概率策略、冷却/去重、可注入 RNG |
| `rusty_moment/detection/detection_pipeline.py` | 采集→切句→识别→打分→触发，跑在独立工作线程 |
| `rusty_moment/core/state_machine.py` | `IDLE→CANDIDATE→TRIGGERED→PLAYING→RESTORING→COOLDOWN→IDLE` 白名单转移 |
| `rusty_moment/core/cooldown_manager.py` | 事件结束后的冷却计时（可注入时钟） |
| `rusty_moment/core/event_manager.py` | 串联触发→切断→播放→恢复→冷却；停止/排队/超时保护 |
| `rusty_moment/core/connection_watcher.py` | 后台线程自动重连 OBS |
| `rusty_moment/ui/*` | 主窗口、设置对话框、事件日志、Qt/线程桥 |
| `rusty_moment/app.py` | 依赖装配与生命周期（唯一注入点） |

### 2.4 线程模型

- GUI 线程：只做界面与配置写入。
- `rusty-detect` 线程（阶段 2）：从麦克风队列取数据 → VAD 切句 → 识别 → 打分，慢操作全在这里。
- `rusty-event-*` 线程：一个事件一条线程，内部严格串行（切断 → 播放 → 等待 → 恢复）。
- `obs-rx` 线程：接收 OBS 消息并派发响应/事件。
- `obs-watcher` 线程：自动重连。
- PortAudio 回调线程（播放与麦克风）：**只做 numpy 拷贝与入队**，不解码、不写文件、不碰 GUI。

---

## 3. 安装

### 3.1 开发环境（源码运行）

```bat
git clone https://github.com/yaosizhan2007-hub/very-rusty-moment-in-my-life.git rusty-moment
cd rusty-moment
setup.bat            :: 创建 .venv 并安装依赖
run.bat              :: 启动（使用 pythonw，无控制台窗口）
```

或者手动：

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

> 发行版的安装/升级/卸载、全新 Windows 检查清单，见 **[docs/INSTALL.md](docs/INSTALL.md)**。
> 性能实测数据与方法见 **[docs/PERF.md](docs/PERF.md)**。

### 3.2 发行版

```bat
.venv\Scripts\python.exe -m PyInstaller packaging\rusty_moment.spec --noconfirm --clean
dist\RustyMoment\RustyMoment.exe
```

`onedir` 目录可整体拷走，目标机器**不需要安装 Python**。若需要单文件 exe，见 `packaging/rusty_moment.spec` 顶部注释（需自行复测启动时间）。

---

## 4. OBS 配置（必需）

1. 安装 OBS Studio 28 以上（本项目在 **OBS 32.2.2 + obs-websocket 5.7.4** 上实测）。
2. 打开 OBS → 菜单 **工具 → WebSocket 服务器设置** → 勾选「启用 WebSocket 服务器」，记下端口（默认 4455）与密码。
3. 让「游戏声音」成为 OBS 里的**独立音源**，二选一：
   - **桌面音频**（默认存在）：会采集整机输出声音，简单但会把浏览器、语音软件一起掐掉。
   - **应用音频采集**（Windows 10 2004+，`应用音频采集` 源）：只采集指定游戏进程，更精准。
4. 确认音源名称：在 OBS 音频混音器里看到的名字，就是本程序「游戏音源」要选的名字（例如 `桌面音频`）。
5. 如果希望 BGM 也从 OBS 内部走（例如直播里观众能听到），把 BGM 作为 OBS 媒体源，程序同样能控制它的音量；否则 BGM 由本程序直接播放到系统输出，OBS 用桌面音频采集即可听到。

> ⚠️ 本程序只控制 **OBS 内部音源**。如果你希望「游戏声音被切断」的同时自己耳机里也听不到，请让游戏声音走 OBS 音源并在 OBS 里监听，或参考第 13 节使用虚拟音频设备分轨。

---

## 5. 从零开始的操作流程

1. 安装 OBS，按第 4 节启用 WebSocket 并确定游戏音源名称。
2. **准备音乐**：程序首次启动会自动扫描系统「音乐」目录，最多收三首作为默认曲目；也可以在主界面点「添加音乐…」或设置 → 音频里添加自己的文件（WAV / MP3 / FLAC / OGG / AAC / M4A）。请只使用自己合法获得的音频。
3. 启动 Rusty Moment（`run.bat` 或 `dist\RustyMoment\RustyMoment.exe`）。
4. 点「设置 → OBS」：填地址 `127.0.0.1`、端口 `4455`、密码；勾选「保存密码」可加密存到本机（DPAPI）。点「测试连接」，看到 `✅ OBS 32.2.2 / obs-websocket 5.7.4` 即成功。
5. 点「刷新音源列表」，在「游戏音源」里选择你的游戏音源（例如 `桌面音频`）；控制策略选「立即静音」或「压低音量」。
6. 点「设置 → 音频」：确认曲目列表（可添加/移除/上移下移/试听），设播放音量与输出设备；回主界面用「悲壮曲目」下拉切换当前曲目，双击设置里的曲目可试听。
7. 回到主界面，点「测试音频」：程序会短暂静音游戏音源再恢复，并试播当前曲目；全部正常会提示「音频测试通过」。
8. 点「设置 → 检测」：勾选「启用本地语音识别」，模型建议 **small**（首次使用会下载约 460MB 到本机，之后离线可用）；选择输入设备、采样率（默认 16 kHz）、VAD 灵敏度；需要时编辑关键词库（每行 `词组|权重`）与权重；保存。
9. 回主界面点「开始监听麦克风」：顶部变成「麦克风：监听中」，下方会显示最近识别到的文本（可在隐私设置里关掉显示）。第一次会加载/下载模型，按钮会短暂显示「启动中…」。
10. 右侧「模式与概率」调节：智能悲壮模式更保守；抽象整活模式会对普通废话按概率抽样。阈值、冷却、概率各档位都在这里。
11. 开始游戏。需要悲壮时按 **F8**（或点「强制触发」）——游戏声立刻消失，BGM 响起；也可以让程序自己听到「别管我」「我尽力了」之类的台词后自动触发。
12. 想提前收场按 **F9**（或点「停止音乐」）——音乐停止，游戏声恢复。
13. 想临时禁止一切触发，勾选「全局静音开关」；只想禁自动触发用 F10。
14. 如果怀疑游戏声没恢复：点「重置音频状态」，程序会按最近一次快照强制恢复；若上次是异常退出，下次启动时也会弹窗提示。

### 5.1 曲目列表与切换

- **默认三首**：首次启动会扫描系统「音乐」目录（`C:\Users\<你>\Music`），把找到的前三首受支持音频收进列表，并清理显示名（优先取「」里的曲名）。
- **切换**：主界面「悲壮曲目」下拉选哪首就是哪首，改完立即生效（下一次触发播放的就是它）。下拉项后面出现「（文件缺失）」说明文件被移动/删除了。
- **添加自己的音乐**：主界面「添加音乐…」可多选；设置 → 音频里能添加、移除、上移、下移、双击试听。
- **格式**：WAV / MP3 / FLAC / OGG 走 libsndfile；AAC / M4A / WMA / OPUS 等交给 PyAV（自带 FFmpeg），所以网易云/QQ 音乐缓存的 `.aac` 也能直接用（前提是没有 DRM）。
- 配置文件里对应 `audio.playlist`（`path` + `title`）与 `audio.current_index`；旧版的单个 `bgm_path` 会在启动时自动迁移成列表的第一首。

### 5.2 只想放歌、不开 OBS（仅本地播歌）

平时跟人开玩笑、不直播也不想开 OBS 时用这个：

1. 主界面右侧「输出方式」选 **「仅本地播歌（不连 OBS）」**（设置 → OBS 页也能切）。
2. 顶部状态变成「输出：仅本地播歌」，「连接 / 重新连接 OBS」按钮自动禁用（不需要连）。
3. 点「开始监听麦克风」，之后命中台词或按 F8 就只放音乐——**不碰任何游戏/系统音频**，也不会自动重连 OBS。
4. 「测试音频」在纯本地模式下只试播当前曲目，不再要求 OBS。

什么时候适合：自己一个人玩、和朋友语音时整活、录屏只想加 BGM 不想动原声。什么时候不适合：你想真的把游戏声音切断——那必须用 OBS 模式。

切回 OBS 模式的步骤反过来：选「OBS 控制」→ 点「连接 / 重新连接 OBS」→ 看到「OBS：已连接」即可。

---

## 6. 音频恢复机制（本项目最关键的逻辑）

触发时程序先记录快照，再动手：

```
快照 = { 音源名, 是否静音, 音量倍数 }
```

恢复时的规则（实现见 `audio_state_manager.py`）：

1. **原本静音的音源，结束后仍然是静音**——绝不会因为「播放完了」就无条件取消静音。
2. **原本不是静音的音源，恢复到原来的音量倍数**，而不是恢复到 100%。
3. **用户在事件期间自己改过静音/音量**：程序检测到当前值和它写入的值不一致，就认定「用户已接管」，跳过该项恢复，并在日志里写明「静音状态已被手动修改」。
4. **先静音再改音量**（原本静音时），避免恢复过程中漏出一瞬间的游戏声。
5. **OBS 断线时不发送任何指令**，保留快照；重连后点「重置音频状态」可以补恢复。
6. **同一时刻只允许一个活动事件**：播放中再次触发默认被忽略（可配置为「排队」），不会叠加播放、不会覆盖快照。
7. **恢复永远发生在音乐真正停止之后**（`wait_until_inactive`），不会出现「音乐还在响游戏声就回来了」。
8. **异常退出兜底**：快照会落盘到 `%APPDATA%\RustyMoment\pending_audio_state.json`，正常恢复后删除；下次启动如果发现残留，会弹窗询问是否恢复。
9. **音乐超过最大时长（默认 300 秒）会强制停止**并恢复音频，避免循环播放把游戏声永久掐掉。

恢复策略可在设置里选择：

- `恢复事件前的原始状态`（默认，推荐）
- `恢复时强制取消静音`（有覆盖风险，仅在你确认「危险场景：音源被自己静音了」时使用）

---

## 7. 快捷键

| 按键 | 功能 |
| --- | --- |
| F8 | 强制触发 |
| F9 | 停止音乐并恢复 |
| F10 | 切换自动触发开关 |

**默认是应用内快捷键**（Rusty Moment 窗口激活时生效），不会抢游戏的按键。

如果需要在游戏窗口里也能按，去「设置 → 检测 → 快捷键」打开 **「注册为全局热键」**：

- 全局热键用 Windows `RegisterHotKey` 实现，**会被本程序独占**——游戏里按同一个键将不再响应游戏；
- 所以默认关闭，且只有你主动打开才注册；注册失败（被其它程序占用）会在日志里提示；
- 键位可以用设置里的输入框直接录制（支持 Ctrl/Alt/Shift/Win + 字母/数字/F1~F24）；
- 冲突时程序会自动禁用重复项并提示。

---

## 8. 配置文件与数据文件

| 路径 | 内容 |
| --- | --- |
| `%APPDATA%\RustyMoment\config.json` | 全部配置（JSON，原子写入，损坏时自动备份为 `config.corrupt-<时间戳>.json`） |
| `%APPDATA%\RustyMoment\secrets.bin` | DPAPI 加密的 OBS 密码（仅当前 Windows 用户可解） |
| `%APPDATA%\RustyMoment\pending_audio_state.json` | 未完成的音频快照（正常恢复后自动删除） |
| `%APPDATA%\RustyMoment\logs\rusty_moment.log` | 运行日志（2MB × 4 滚动，密码/密钥自动脱敏） |

**配置文件损坏**：程序会提示并自动恢复默认配置继续启动，原文件保留为备份，不会直接崩溃。

---

## 9. 隐私

- 麦克风必须由你手动启用（阶段 2 才有采集功能，本版本完全不采集音频）。
- 默认不保存原始录音、不上传音频。
- 本地语音识别只用于判断（阶段 2/3），默认不落盘完整转录文本。
- 若将来启用云端 AI 分析：只发送识别出的文本，不含音频；必须在设置里单独勾选同意，且可随时关闭。
- 日志中不记录密码、API Key（自动替换为 `***REDACTED***`），默认不记录语音内容。
- 关闭程序会释放音频设备与麦克风；程序不会隐藏运行。

### 9.1 它会听到队友说话吗？

取决于你选的**输入设备**：

| 设备类型 | 会不会听到队友 |
| --- | --- |
| 真实麦克风（麦克风阵列 / USB 麦克风 / 耳机麦克风） | **不会**。只拾取物理传到麦克风的声音；戴耳机时队友语音从耳机出，不会被拾取 |
| 立体声混音 / 「…with SST」/ Loopback / What U Hear | **会**。这类"输入设备"录的其实是系统输出，队友语音、游戏音效全都在里面 |

- 程序只打开你在设置里选的那一个设备（默认是系统默认输入），**不会去抓其他应用的声音**。
- **外放**（不戴耳机）时，扬声器放出的队友语音会被麦克风物理拾取，这种情况程序无法区分是队友还是你。
- 即使用的是真实麦克风，**程序也无法区分说话人**：队友如果喊出同样的台词（比如他也喊「别管我」），一样会触发。这是内容识别的固有限制，不是 bug。
- 设置 → 检测的设备列表里，回环设备会标注「⚠ 会录到系统声音，含队友语音」；如果你选了它并开始监听，程序会弹一次提醒。
- 想彻底避免：用耳机 + 真实麦克风；或把游戏内语音音量调低；或干脆用「抽象整活」模式当纯娱乐，别在意误触发。

---

## 10. 自动化测试

```bat
.venv\Scripts\python.exe -m pytest -q
```

**本机实测结果（2026-10-22，Python 3.13.13 / Windows 11 25H2）：`237 passed in 13.16s`**

覆盖范围：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_obs_client.py` | 握手、密码认证、错误密码、请求-响应、超时、服务端断开、Exiting 事件 |
| `test_obs_controller.py` | 音源枚举与过滤、音源改名报错、静音/音量读写、静音与压低策略 |
| `test_audio_state.py` | 正常恢复、原本静音保持、用户中途改动静音/音量、断线保留快照、双重触发拒绝、异常退出留档、强制恢复 |
| `test_state_machine.py` / `test_cooldown.py`（同文件） | 合法/非法状态转移、强制复位、回调异常隔离、假时钟冷却 |
| `test_audio_player.py` | 解码、缺失文件、非法格式、暂停/恢复/停止、音量边界、设备解析、采样率回退重采样、回调结束 |
| `test_config.py` | 默认生成、损坏备份、未来版本拒绝、类型纠偏、权重归一化、概率边界、快捷键冲突、AI 开关纠偏、无明文密码 |
| `test_event_manager.py` | 手动触发全流程、播放中重复触发被忽略、停止后恢复、OBS 断线降级本地播放、播放失败回滚、全局静音、自动触发开关、排队策略、冷却计时、结束后不重启 |
| `test_logging_setup.py` | 密码/API Key 脱敏 |
| `test_vad.py` | 切句边界、过短丢弃、最长会话截断、flush 收尾、噪声底自适应与启动学习期、音量衰减/停顿特征、块大小无关性 |
| `test_keyword_detector.py` | 命中权重、排除词打折、最小命中数、大小写、关闭层、空文本 |
| `test_rule_analyzer.py` | 长台词 vs 短促喊叫、单分量主导、权重全 0、分数范围、低语音占比提示 |
| `test_trigger_engine.py` | 权重归一化、层开关、AI 回退概率、两模式概率差异、冷却、去重窗口、语音过短、RNG 可复现、权重全 0 兜底 |
| `test_detection_pipeline.py` | 合成音频跑完整链路、普通聊天不触发、识别失败不崩、STT 关闭时不调用识别器、CANDIDATE 状态流转、停止释放设备、VAD 灵敏度映射 |
| `test_speech_recognizer.py` | 重采样、结构化评分校验（缺字段/越界 clamp）、配置映射、假模型转写与失败、置信度影响 |
| `test_text_normalize.py` | 繁体转简体、标点归一、繁体转录命中简体关键词（真机故障复现） |
| `test_decoder.py` | WAV/MP3 走 libsndfile、AAC 走 PyAV（真实编码再解码）、损坏/空/缺失文件的错误、时长探测 |
| `test_music_library.py` | 标题清洗（「」提取 / 去尾部 ID / 截断）、目录扫描过滤、默认最多三首、缺目录返回空 |
| `test_diagnostics.py` | 自检输出、退出码、把结果写进日志目录、曲目缺失/可解码两条分支 |
| `test_ai_backends.py` | URL 归一化、JSON 提取（含代码块/前后缀）、请求构造与上下文、401/404/500/超时/连接失败映射、content 空时回退 reasoning、10 分制归一化、配置工厂 |
| `test_global_hotkeys.py` | 热键解析（修饰键顺序/全角加号/F1~F24/非法键）、配置映射、真实注册与注销、非法序列只报告不抛异常 |
| `test_perf_probe.py` | 进程采样（CPU/内存）、VAD 离线耗时、报告落盘、无设备时跳过 |
| `test_gui_smoke.py` | 主窗口构建、按钮触发事件、模式切换写配置、曲目下拉切换写配置、设置里曲目增删排序 |

### 真实环境验证记录

| 项目 | 结果 |
| --- | --- |
| OBS 连接 | OBS 32.2.2 / obs-websocket 5.7.4，连接成功，音源枚举到 `桌面音频 / 麦克风 / 窗口采集` |
| 静音→恢复 | 原始 `muted=False, volume=0.6959` 恢复后完全一致 |
| 压低→恢复 | 压到 0.25 后恢复到 0.6959 |
| 真实播放 | 用 sounddevice 播放 1.2 秒测试音，进度到达 1.20/1.20 秒 |
| 全流程（进程内 GUI） | 自动重连 → 强制触发 → 静音 → 播放 → 恢复 → 冷却 → 状态机回到 idle，事件日志写入 1 条，退出清理正常 |
| 纯本地播歌模式（不开 OBS） | 真实曲目播放成功、`obs_controlled=False`、OBS 控制调用 **0 次**、无音频快照残留、`test_audio` 走纯本地分支通过 |
| 曲目预热 | 首次播放（含 302 秒 AAC 解码）1012ms → 切曲后台预解码 791ms → 预热后播放 **142ms** |
| 触发到播放延迟 | 约 **288 ms**（含 OBS 静音请求往返与音频流启动） |
| DPAPI | 密码加密写入后换一个 `SecretStore` 实例读取，值一致 |
| PyInstaller 打包 | onedir 产物 148MB（瘦身后 109MB），offscreen 启动 10 秒进程存活，自动生成配置与日志 |
| PyInstaller 打包（含 PyAV + 语音识别 + httpx） | onedir 约 339MB，瘦身后 292.8MB；`RustyMoment.exe --check` 退出码 0：PyAV 19.0.2 / faster-whisper 1.2.1 / httpx 0.28.1 全部可用，三首 AAC 解码成功，offscreen 启动 10 秒存活 |
| 性能测量（`--perf`） | 空闲 CPU 0~0.5% / 内存 19MB；麦克风采集 5.2 秒 CPU 2.4% / 内存 46MB / 167 块 0 丢弃；VAD 约 800~925× 实时；播放启动 165~172ms；tiny 识别单句 351ms；详见 `docs/PERF.md` |
| 全局热键 | `Ctrl+Alt+F13` 真实注册与注销成功；非法序列只报告不抛异常（默认仍是应用内快捷键） |

### 阶段 2 真机验证（2026-10-22）

语音素材：Windows 中文 TTS（System.Speech / Microsoft Huihui）生成的 4 句 wav，
覆盖「明显悲壮 / 普通聊天 / 带排除词的玩笑 / 关键词误导+真实悲壮」四类。

| 素材 | 内容（TTS 原句） |
| --- | --- |
| bizhuang | 兄弟，别管我，你们快走，我来拖住他们。 |
| normal | 今天中午吃什么，要不点个外卖吧。 |
| joke | 别管我，开玩笑的哈哈。 |
| hero | 我尽力了，至少我拖住他们了，替我报仇。 |

识别（本地 faster-whisper, int8, CPU）：

| 模型 | 加载耗时 | 单句识别延迟（3~5 秒音频） | 转写正确率 | 备注 |
| --- | --- | --- | --- | --- |
| tiny | 1.5s（已缓存） | 373~470 ms | 2/4 完全正确 | hero 句错成「我进立了…托主」，但关键词仍命中 |
| small | 2.6s（已缓存） | 2118~2350 ms | 4/4 完全正确 | 默认推荐；首句前需下载约 460MB |

决策结果（阈值 0.6、最短语音 0.6s、冷却默认）：

| 素材 | 合成分 | 关键词分 | 规则分 | 是否触发 | 是否符合预期 |
| --- | --- | --- | --- | --- | --- |
| bizhuang | 0.89 | 1.00 | 0.68 | ✅ 触发 | 是 |
| normal | 0.22 | 0.00 | — | ❌ 不触发 | 是 |
| joke | 0.28 | 打折后 | — | ❌ 不触发 | 是（排除词「开玩笑」生效） |
| hero | 0.89 | 1.00（替我报仇） | 0.68 | ✅ 触发 | 是 |

**真机发现并修掉的两个问题**（都已补测试）：

1. Whisper 对中文会输出**繁体**（「別管我」），而关键词库是简体 → 关键词层整层失效。
   修法：① 给模型加 `initial_prompt="以下是普通话的句子。"`（实测能把输出压回简体）；
   ② 再加一层 `text_normalize.to_simplified()`（Windows `LCMapStringW`，带内置兜底表），关键词匹配前统一繁转简。
2. 识别的第一版把尾部静音算进了语音段，导致「越说越小声」这类特征被静音污染 →
   改为切句时裁掉尾部静音（保留 150ms 缓冲）。

两种模式的差异（同一句普通聊天，规则分 0.3、关键词 0）：

| 模式 | 合成分 | 概率档 | 触发 |
| --- | --- | --- | --- |
| serious | 0.10 | below_low（0%） | 否 |
| chaotic（chaotic_low_score=0） | 0.10 | chaotic_low（0%） | 否 |
| chaotic（chaotic_low_score=1.0） | 0.10 | chaotic_low（100%） | 是 |

真实麦克风（不做任何回放，安静环境）：

| 项目 | 结果 |
| --- | --- |
| 设备枚举 | 14 个输入设备（含 Realtek 麦克风阵列 / 麦克风输入） |
| 打开 16 kHz 单声道流 | 成功 |
| 4 秒采集 | 133 块、0 丢弃 |
| 环境电平 | mean -60.9 dB、peak -54.6 dB |
| VAD 结果 | 0 段语音（无人在说话时不误报） |
| 停止后 | 流已释放，设备枚举仍正常 |

### 曲目列表与 AAC 解码验证（2026-10-22）

| 项目 | 结果 |
| --- | --- |
| 默认曲目发现 | 扫描 `%USERPROFILE%\Music` 得到 3 首本地 aac，标题清洗正常（优先取「」里的曲名，去掉尾部编号） |
| 解码 | 三首 `.aac` 全部由 PyAV 解码成功：216.3s / 302.3s / 245.8s，48 kHz 立体声，单首 0.47~0.66s |
| 真实播放 | 音量 5% 播放 1.5 秒，position 到 1.45s，停止后 state=idle |
| 切换曲目 | 从「曲目 A」切到「曲目 B」：重新解码并播放，切换+解码 863ms |
| 触发链路 | 手动触发播放「当前曲目」，`max_event_seconds=2s` 到时强制停止并走恢复流程 |

### 阶段 3 真机验证（本地 Ollama / qwen3:8b，CPU int8）

| 素材 | AI 悲壮分 | 上下文 | 置信 | 合成 AI 分 | 延迟 | 模型判断理由 |
| --- | --- | --- | --- | --- | --- | --- |
| 兄弟，别管我，你们快走，我来拖住他们。 | 0.80 | 0.50 | 0.70 | 0.50 | 8.9s | 游戏牺牲宣言 |
| 今天中午吃什么，要不点个外卖吧。 | 0.10 | 0.30 | 0.90 | 0.14 | 5.4s | 普通闲聊 |
| 别管我，开玩笑的哈哈。 | 0.10 | 0.60 | 0.70 | 0.17 | 6.7s | 轻松玩笑 |
| 我尽力了，至少我拖住他们了，替我报仇。 | 0.80 | 0.60 | 0.80 | 0.59 | 8.6s | 游戏认输告别 |

超时与失败路径也实测过：`timeout=0.3s` 时约 1.4 秒抛出「AI 请求超时」；连不上的地址抛「AI 请求超时」；缺字段/越界数据被 `parse_scores` 拦下。

**真机踩到的三个坑（都已修 + 补测试）**：

1. **思考型模型把输出预算吃光**：qwen3 先输出 500+ 字的思考，`max_tokens=220` 时 `content` 直接是空字符串 → 修法：默认 `max_tokens=1200`，并在 `content` 为空时回退到 `reasoning` 字段。
2. **`response_format={"type":"json_object"}` 在 Ollama 兼容层上有坑**：带上它之后思考内容会混进 `content`，解析必失败 → 改为默认不发，靠 prompt 约束 + 本地 JSON 提取兜底（`use_json_mode` 可选）。
3. **模型用 10 分制打分**：真实返回过 `dramatic_score=1, context_score=3`，旧的 clamp 会把 3 变成 1.0（普通聊天被判成满分悲壮）→ 现在只要任一分数 > 1 就整体除以 10，宁可估低也不乱切音乐。

---

## 11. 打包

见 `packaging/rusty_moment.spec`（onedir）。已实测可生成并启动 `dist/RustyMoment/RustyMoment.exe`：

```bat
.venv\Scripts\python.exe -m PyInstaller packaging\rusty_moment.spec --noconfirm --clean
dist\RustyMoment\RustyMoment.exe          :: 应能在无 Python 环境的机器上直接运行

:: 可选瘦身：删除用不到的 Qt 组件（实测 148MB → 109MB，删完需重新验证启动）
.venv\Scripts\python.exe packaging\slim_after_build.py
```

打包注意事项：

- `config/default_config.json` 会打进 `_internal/config/`，程序会优先读它；文件缺失时退回代码内置默认值。
- 使用 `console=False`，运行日志只写文件，不弹控制台。
- `slim_after_build.py` 会删除 `opengl32sw.dll`（软件 OpenGL 回退）与 Quick/Qml/Pdf 组件；如果目标机器是**没有显卡驱动的虚拟机**，请保留 `opengl32sw.dll` 或跳过瘦身，否则界面可能起不来。
- 若杀毒软件误报，先确认来源，再考虑签名或改用 onedir + 白名单。

---

## 12. 故障排查

| 现象 | 检查 |
| --- | --- |
| 提示「OBS 未连接」 | OBS 是否运行；工具 → WebSocket 服务器设置是否启用；端口/密码是否一致；Windows 防火墙是否拦了本机回环（一般不需要） |
| 提示「OBS 中找不到音源 X」 | 音源被改名/删除了。进设置点「刷新音源列表」重新选择；程序不会静默控制错对象 |
| 只能听到 BGM，游戏声音没被切断 | 游戏声音不在 OBS 的音源里（比如游戏绕过了桌面音频采集）；或控制策略是「压低音量」且值设置过高 |
| 游戏声音一直静音 | 点「重置音频状态」；检查是否开了「恢复时强制取消静音」策略下的异常；查看日志中的恢复结果 |
| BGM 没声音 | 设置里「试听」是否正常；输出设备是否选对（蓝牙耳机切换设备后索引会变） |
| 提示「不支持的格式」 | 只保证 WAV / MP3 / FLAC / OGG；其他格式请先转码 |
| 配置损坏提示 | 程序已把原文件备份为 `config.corrupt-*.json` 并恢复默认，重新配置即可 |
| 日志太大 | 日志按 2MB×4 滚动，可在设置里调级别或删除 `%APPDATA%\RustyMoment\logs` |
| 想知道这台机器到底能不能放 AAC / 能不能识别 | 命令行运行 `RustyMoment.exe --check`（源码：`python main.py --check`），结果同时写到 `%APPDATA%\RustyMoment\logs\self-check.txt` |
| 队友说话也会触发 | 你的输入设备可能选成了「立体声混音 / …with SST / Loopback」（会录到系统输出），或者你在外放导致麦克风拾取。详见 9.1 |

---

## 13. 已知限制

1. **只控制 OBS 内部音源**，不能直接切断 Windows 中游戏自身的声音。`SystemAudioBackend` 接口已留，但未实现（需要真机验证 Core Audio 会话控制或虚拟音频设备，见 `audio_router.py` 注释）。**「仅本地播歌」模式则完全不碰音频设备**（除了它自己的播放输出）。
2. **AI 语义层默认关闭，需要你自己配**：本地要装 Ollama 并拉一个模型（实测 qwen3:8b 在 CPU 上 5~9 秒/句，太慢就不适合实时；有 GPU 会快很多），云端要填你自己的 API Key（存 DPAPI）。云端后端**未做真机联调**（本机没有可用 key），只用 MockTransport 与本地 Ollama 验证过协议与错误路径。
3. **语音识别需要本地模型**：首次启用会从 Hugging Face 下载（tiny ≈ 75MB / base ≈ 145MB / small ≈ 460MB），要求能访问该站点；之后可离线使用。识别质量与延迟取决于模型和 CPU（本机 tiny ≈ 0.4s、small ≈ 2.2s / 句）。
4. **发行版体积约 293MB**：内置了 PyAV（FFmpeg，用于 AAC/M4A 解码）、faster-whisper / ctranslate2 / onnxruntime（本地语音识别）与 httpx（AI 层）。语音模型本身仍要首次下载。如果你只需要播放/OBS 功能，可以在 spec 里排除这些依赖重新打包——但注意**排除 PyAV 后 `.aac` 就放不了**（默认曲目正是 aac）。
5. **关键词库是示例**：默认词库只覆盖常见牺牲台词，识别准不准很大程度取决于你自己往里加的词。程序不保证「识别率」，README 也不宣称任何准确率数字。
6. **快捷键默认是应用内快捷键**；可在设置里打开全局热键（`RegisterHotKey`，会独占按键、可能和游戏冲突，默认关闭）。
7. **音频格式**：WAV / MP3 / FLAC / OGG 走 libsndfile，AAC / M4A / WMA / OPUS 等走 PyAV（FFmpeg）。实测本机三首 `.aac` 全部可解码播放；带 DRM 的音频仍然放不了。
8. **本程序只测过一个场景集合**（桌面音频 + 麦克风 + 窗口采集），OBS 里的「应用音频采集」源需要你自行确认音源名称。
9. **异常退出无法保证恢复**：只提供下次启动时的提示与手动恢复。若 OBS 在此期间被重启，源状态以 OBS 当前值为准。
10. **播放器把整首 BGM 解码进内存**（上限 400MB），5 分钟的立体声约 110MB；超长音频会提示先转码。
11. 配置与凭据都按当前 Windows 用户存储；换用户/换机器后 DPAPI 解不开 OBS 密码，需要重新输入。
12. **VAD 是能量型**：风扇突然变大、机械键盘猛敲、音乐外放都可能被切成语段；已用启动学习期、最短语音时长和关键词/规则双证据来压制，但不能承诺零误触发。噪声大的环境请调低 VAD 灵敏度。
13. **模块级进度**：阶段 1~4 的功能都已实现并有自动化测试；还没验证的是「Windows 10 真机」「无 Python 的干净机器」「云端 AI 真实 key」，清单写在 `docs/INSTALL.md`。

---

## 14. 后续开发计划

阶段 1~4 的主体都已完成。剩下的都是「需要特定环境才能验证/继续」的项：

- **云端 AI 联调**：需要一份真实 API Key（当前只有本地 Ollama + MockTransport 验证）。
- **Windows 10 真机**：本机只有 Windows 11。
- **「无 Python 的干净机器」验证**：本机装了 Python，无法证明 PyInstaller 产物在纯环境可用（理论上 onedir 自带解释器与 DLL）。
- **可选的体验增强**：曲目随机/循环策略、按曲目单独设置音量、OBS 场景切换联动、`onefile` 打包评估。

已实现且可继续调参的部分（不需要改代码）：

```
combined = Σ (w_i / Σw_active) * score_i      # 只对启用的层归一化
serious:  >= 主阈值 → serious 概率；>= 低置信阈值 → low_confidence 概率；再低无资格
chaotic:  >= 主阈值 → chaotic 概率；低于主阈值 → chaotic_low_score 概率
AI 启用但结果缺失 → 达标时用 ai_unavailable 概率
最终：概率抽样 → 冷却检查 → 去重 → 触发
```

---

## 附录 A：识别与评分机制（实现细节）

### A.1 切句（`vad.py`）

- 帧长 30ms，逐帧算 RMS→dBFS。
- 噪声底 = 最近 200 帧的 20 分位；阈值 = `max(absolute_floor_db, 噪声底 + 8dB)`。
- **启动学习期 500ms**：只收噪声样本，除非响度比噪声底高 15dB 以上才允许判语音（避免一开机把环境噪声当台词）。
- 一句话 = 连续语音 + 结束静音 `>= 500ms`；语音本身 `>= 400ms`（可由「最短语音时长」配置放大）；单句上限 15s。
- 切句时裁掉尾部静音（保留 150ms），避免「越说越小声」被静音污染。
- 特征：时长、语音占比、平均/峰值/尾部电平、音量衰减比、句内最长停顿、突然拔高幅度。

### A.2 关键词层（`keyword_detector.py`）

```
positive = min(1, Σ命中词权重 / 1.0)
penalty  = min(0.9, Σ排除词权重 × 0.8)
score    = positive × (1 − penalty)
matched  = 命中词数 >= min_hits 且 score > 0
```

匹配前先做繁转简与标点归一（`text_normalize.py`），大小写可按配置折叠。

### A.3 规则层（`rule_analyzer.py`）

| 分量 | 计算 | 权重（默认） |
| --- | --- | --- |
| `speech_duration` | `min(1, 时长 / 4s)` | 0.25 |
| `pause_after` | `min(1, 句内最长停顿 / 1.2s)` | 0.25 |
| `volume_drop` | 尾部相对峰值的衰减比 | 0.20 |
| `volume_spike` | `min(1, 峰值−均值 dB / 12)` | 0.10 |
| `silence_after` | `1 − (尾部 dB − (−60)) / (峰值 − (−60))` | 0.20 |

五个分量按权重归一化后加权平均。**没有任何单一分量能独立触发**。

### A.4 合成与概率（`trigger_engine.py`）

```
active = {层 | 该层启用且有结果}
combined = Σ (w_i / Σw_active) × score_i
```

- 关闭的层、AI 缺失（未启用/超时/非法返回）→ 不参与归一化，权重自动重分配。
- 有结果但 0 分的层算**反面证据**（例如关键词完全没命中），会拉低合成分。
- 分档：`serious`（≥ 主阈值）/ `low_confidence`（≥ 低置信阈值）/ `below_low`（无资格）；
  `chaotic`（≥ 主阈值）/ `chaotic_low`（低于主阈值，仍可抽样）。
- AI 启用但结果缺失 → 达标时用 `ai_unavailable` 概率。
- 抽样用注入的 `random.Random`（测试可复现），概率是对「满足条件的候选事件」抽样，不是定时器。

### A.5 防误触发清单（对应阶段 2 验收）
| 机制 | 位置 |
| --- | --- |
| 最短语音时长 | `TriggerEngine` + VAD `min_speech_ms` |
| 同句去重（窗口可配） | `DedupeTracker` |
| 冷却（从事件结束算） | `CooldownManager` + `TriggerEngine` |
| 自动触发总开关 / 全局静音 | `EventManager.start_event` |
| 事件进行中禁止并发触发 | `EventManager.is_busy`（可配 `ignore`/`queue`） |
| 识别失败回退（无文本，只靠规则） | `DetectionPipeline` |
| 关键词不能单独定性 | 权重归一化 + 阈值 + 排除词打折 |
| 安静环境不误报 | VAD 启动学习期（真机 4 秒静音 0 段语音） |

### A.6 AI 层（可选，阶段 3）

- **只发文本**：当前句 + 最近 `ai.max_context` 句（默认 3），从不发音频；云端后端必须先在隐私页勾选同意。
- **请求**：`POST {base_url}/v1/chat/completions`，`temperature=0`、`max_tokens=1200`，`messages=[system(提示词), user(上下文+当前句+JSON 示例)]`。
- **不发 `response_format`**：Ollama 兼容层带这个字段会把思考内容混进 `content`（实测），因此靠提示词约束 + 本地提取。
- **取内容顺序**：`choices[0].message.content` → 空则回退 `reasoning_content`/`reasoning` → 用正则抠第一个 JSON 对象（允许 ```json 包裹）。
- **校验**：缺字段 / 非对象 / 无法解析 → `AnalysisError`；任一分数 > 1 视为 10 分制，整体除以 10；`reason` 截断 200 字。
- **合成分**：`(0.7 × 悲壮 + 0.3 × 上下文) × max(0.2, 置信度)`。
- **失败处理**：超时 / 401 / 404 / 非法数据 → 记日志、该层不参与权重归一化；达标时改用 `ai_unavailable` 概率档。

---

## 15. 版权与分发

- 仓库不含音乐文件，不自动下载音乐，不上传音乐，`*.mp3` / `*.aac` / `*.wav` 等已在 `.gitignore` 里排除。
- 用户须自行提供合法获得的音频；公开分发前请自行确认商用/公开发布所需的音乐授权。
- 本项目与音乐版权方、动画制作方、发行方无任何官方合作关系，README 里出现的曲名只用于说明「你可以放什么」。
- 代码采用 **MIT 许可证**，见 `LICENSE`。
- 第三方依赖遵循各自许可证：PySide6 (LGPLv3)、sounddevice (MIT)、libsndfile (LGPLv2.1)、PortAudio (MIT)、numpy (BSD-3-Clause)、websocket-client (Apache-2.0)、PyAV/FFmpeg (BSD/LGPL)、faster-whisper (MIT)、ctranslate2 (MIT)、onnxruntime (MIT)、httpx (BSD-3-Clause)、pywin32 (PSF-2.0)、PyInstaller (GPLv2 with bootloader exception)。

---

项目结构：

```
rusty-moment/
├── main.py                     # 入口
├── run.bat / setup.bat         # 一键启动 / 安装
├── requirements.txt
├── README.md / LICENSE
├── config/default_config.json  # 默认配置模板（schema v2）
├── packaging/rusty_moment.spec # PyInstaller 配置
├── docs/INSTALL.md             # 安装/升级/卸载 + 全新环境清单
├── docs/PERF.md                # 性能实测记录
├── rusty_moment/
│   ├── app.py                  # 依赖装配 + 生命周期
│   ├── config/config_manager.py
│   ├── security/secret_store.py
│   ├── audio/{audio_player,audio_state_manager,audio_router,decoder,music_library}.py
│   ├── detection/{microphone_capture,vad,speech_recognizer,text_normalize,
│   │              keyword_detector,rule_analyzer,emotion_analyzer,ai_backends,
│   │              trigger_engine,detection_pipeline}.py
│   ├── obs/{obs_client,obs_controller}.py
│   ├── core/{event_manager,state_machine,cooldown_manager,connection_watcher,global_hotkeys}.py
│   ├── ui/{main_window,settings_dialog,event_log_widget,qt_bridge}.py
│   ├── utils/logging_setup.py
│   ├── diagnostics.py          # --check 自检
│   └── perf_probe.py           # --perf 性能测量
└── tests/                      # 232 个自动化测试
```
