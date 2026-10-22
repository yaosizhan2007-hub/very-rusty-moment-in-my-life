# Rusty Moment 安装 / 升级 / 卸载

> 面向两种人：**只想用**（装发行版）和**要改代码**（开发环境）。
> 完整功能说明见 [README](../README.md)。

---

## 一、用发行版（不需要装 Python）

### 1. 系统要求

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10 1903+ / Windows 11（x64） |
| 磁盘 | 约 300MB（含内置的 FFmpeg、语音识别依赖） |
| 内存 | 空闲约 50MB；开启语音识别 + 本地 AI 时按模型大小另算（tiny ≈ 0.3GB、small ≈ 1GB、8B LLM ≈ 6GB） |
| OBS | OBS Studio 28 以上（内置 obs-websocket 5.x），本项目在 32.2.2 实测 |
| 网络 | **仅首次启用语音识别/AI 时需要**：下载识别模型（tiny 75MB / small 460MB）或调用云端 API |

### 2. 安装

1. 解压 `RustyMoment/` 整个目录（不要只拷 exe，它依赖 `_internal/`）。
2. 双击 `RustyMoment.exe`。
3. 首次启动会在 `%APPDATA%\RustyMoment\` 生成配置，并自动把系统「音乐」目录里的前三首音频收进曲目列表。
4. 按 README 第 4~5 节配置 OBS 与音源。

> 首次运行如果 Windows 弹出「已保护你的电脑」，这是未签名 exe 的正常提示，点「更多信息 → 仍要运行」。

### 3. 自检与排错

```bat
RustyMoment.exe --check     :: 环境自检，结果写到 %APPDATA%\RustyMoment\logs\self-check.txt
RustyMoment.exe --perf      :: 性能测量，结果写到 …\logs\perf-report.md
```

`--check` 会逐项告诉你：音频后端、AAC/M4A 解码、语音识别依赖、DPAPI、以及三首默认曲目能不能解开。发问题反馈时请附上这个文件。

### 4. 升级

直接解压覆盖 `RustyMoment/` 目录即可，配置与凭据都在 `%APPDATA%\RustyMoment\`，不会丢。

配置版本会自动迁移（v1 的单曲路径 → v2 的曲目列表）；迁移失败或配置损坏时，程序会备份为 `config.corrupt-<时间>.json` 并提示恢复默认。

### 5. 卸载

1. 关闭程序（任务栏托盘/窗口关闭即可；关闭时会释放麦克风与音频设备）。
2. 删除解压出来的 `RustyMoment/` 目录。
3. 删除数据目录（**会清掉配置、日志、加密保存的 OBS 密码 / API Key**）：

```bat
rmdir /s /q "%APPDATA%\RustyMoment"
```

本项目**不写注册表、不开机自启、不装服务**。卸载后可以用任务管理器确认没有残留的 `RustyMoment.exe` 进程。

---

## 二、开发环境

### 1. 安装

```bat
git clone https://github.com/yaosizhan2007-hub/very-rusty-moment-in-my-life.git rusty-moment
cd rusty-moment
setup.bat                              :: 建 .venv + 装 requirements.txt
.venv\Scripts\python.exe main.py       :: 启动
.venv\Scripts\python.exe -m pytest     :: 跑测试
```

已验证组合：**Python 3.13.13 + Windows 11 25H2**，依赖版本见 `requirements.txt` 顶部注释。

### 2. 打包

```bat
.venv\Scripts\python.exe -m PyInstaller packaging\rusty_moment.spec --noconfirm --clean
.venv\Scripts\python.exe packaging\slim_after_build.py     :: 可选瘦身（约 -46MB）
dist\RustyMoment\RustyMoment.exe --check                   :: 打包后先自检
```

注意：`slim_after_build.py` 会删掉 `opengl32sw.dll`（软件 OpenGL 回退），**没有显卡驱动的虚拟机**请跳过瘦身。

### 3. 目录约定

| 路径 | 内容 |
| --- | --- |
| `%APPDATA%\RustyMoment\config.json` | 全部配置（原子写入） |
| `%APPDATA%\RustyMoment\secrets.bin` | DPAPI 加密的 OBS 密码 / AI Key |
| `%APPDATA%\RustyMoment\pending_audio_state.json` | 未完成的音频快照（正常恢复后自动删除） |
| `%APPDATA%\RustyMoment\logs\` | 运行日志、`self-check.txt`、`perf-report.md` |
| `%APPDATA%\RustyMoment\recordings\` | 只有你手动开启「保存原始录音」时才有内容 |

---

## 三、全新 Windows 环境检查清单

在干净的 Windows 10/11 上分发前逐项确认（✅ = 本机已验证，⬜ = 需要你在干净机器上补验）：

| 检查项 | 状态 |
| --- | --- |
| exe 能在 Windows 11 启动并显示界面 | ✅（本机） |
| 不需要系统安装 Python（onedir 自带解释器与 DLL） | ✅（`_internal/python313.dll` 随包分发） |
| 首次启动自动生成配置、收进三首默认曲目 | ✅ |
| 关闭再启动后配置仍在（含曲目列表与当前曲目） | ✅ |
| 缺少语音模型时给出明确提示而不是崩溃 | ✅（`--check` 与启动提示） |
| OBS 未启动时程序正常运行、只是提示未连接 | ✅ |
| 依赖缺失（如 PyAV 被删）时提示而非静默失败 | ✅（`--check` 会标 ❌） |
| Windows 10 真机启动 | ⬜ 未验证（本机只有 Windows 11） |
| 无 Python 的干净虚拟机启动 | ⬜ 未验证（本机装了 Python，无法证明「干净」） |
| 卸载后无残留进程、无自启项 | ✅（进程级验证；未写注册表/服务） |
| 杀毒软件误报情况 | ⬜ 未验证（无签名） |

---

## 四、已知安装/分发限制

- **未签名**：SmartScreen 会提示；要发布给他人建议自签名或代码签名证书。
- **体积约 293MB**：主要来自 PySide6(93MB) + FFmpeg/PyAV + faster-whisper/ctranslate2/onnxruntime。
- **语音模型不随包分发**：首次启用时下载到本机缓存；离线环境需要提前把模型放进「设置 → 检测 → 模型目录」。
- **只支持 x64**：没有 32 位 / ARM 构建。
