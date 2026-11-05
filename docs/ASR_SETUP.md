# 配置完全本地的语音输入

## 准备环境

基础互传不依赖本页。语音识别按需加载，进入“语音输入”页才会初始化模型。

| 项目 | 要求/说明 |
| --- | --- |
| 系统 | Windows 10/11 64 位，支持 Tkinter 的 Python；建议新建 3.10/3.11 环境 |
| 实际开发环境 | Python 3.9.13、faster-whisper 1.1.1、CTranslate2 4.6.0、RTX 5060 Laptop 约 8 GB 显存 |
| 模型 | 已转换为 faster-whisper/CTranslate2 格式的多语言模型，实际复用 large-v3-turbo；不接受只改文件夹名的原始模型 |
| GPU | 可选；本项目使用的版本组合按 CUDA 12 + cuDNN 9 路线配置，并需匹配当前显卡的驱动/运行库 |
| CPU | 配置 `device=cpu`、`compute_type=int8`；保留兼容路径，未在新机器进行速度验收 |
| 麦克风 | 可以用电脑已有输入设备；手机麦克风需另装 AudioRelay 手机/Windows 客户端及其虚拟设备 |

GPU 依赖取决于 **CTranslate2 的具体版本**，不要把任意版本 CUDA/cuDNN 混用。本项目固定包版本，参照 [faster-whisper v1.1.1 的 GPU 要求](https://github.com/SYSTRAN/faster-whisper/tree/v1.1.1#gpu)；升级 CTranslate2 时重新核对官方要求。当前 [CTranslate2 安装文档](https://opennmt.net/CTranslate2/installation.html) 可能对应更新版本。

本项目没有测出可通用的最低内存、显存或实时速度要求，因此不编造“所有电脑都实时”的承诺。磁盘需要容纳 Python 环境和所选模型，具体大小以模型发布页为准。

## 安装 ASR 依赖

在项目根目录执行，复用 README 中的虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-asr.txt
```

依赖安装需要网络；安装完成并准备好模型后，转写本身可离线。当前录音通过 sounddevice 直接传 NumPy 音频，不要求手动安装 FFmpeg；faster-whisper 使用的 PyAV 自带相关解码库，见 [上游要求](https://github.com/SYSTRAN/faster-whisper/tree/v1.1.1#requirements)。

## 配置已有模型，避免重复下载

1. 优先查找已有 faster-whisper 模型目录。
2. 复制 `asr/config.example.json` 为 `asr/config.local.json`。
3. 把 `model_path` 改为已有模型绝对路径，或者相对 `asr` 的路径。

GPU 示例：

```json
{
  "model_path": "D:/Models/faster-whisper-large-v3-turbo",
  "device": "cuda",
  "compute_type": "float16"
}
```

CPU 示例直接使用 `config.example.json` 的 `cpu/int8`。模型目录需要 `model.bin`、配置、tokenizer 与模型发布者提供的必要文件。仓库不自动拉取模型；缺文件时会报错，需自行从可信来源获取完整转换模型并确认其许可证。

## AudioRelay 怎么连接

1. 手机开热点，电脑连接该热点；或两端连接同一局域网。
2. 手机 AudioRelay 的服务端页面选择麦克风作为音源。
3. 电脑 AudioRelay 的播放器页面选择 **Mic/麦克风** 模式，再连接手机。
4. 进入工作台“语音输入”，选择 **Virtual Mic for AudioRelay**，优先使用 Windows WASAPI 项。
5. 电脑播放设备继续使用物理扬声器，无需把默认扬声器改为虚拟设备。

按钮名称以安装版本为准，参照 [AudioRelay 官方手机麦克风说明](https://audiorelay.net/docs/windows/use-your-phone-as-a-mic-for-windows)。AudioRelay 不随本项目分发。手机文件互传与 AudioRelay 音频连接是两条链路，扫码不能代替 AudioRelay 建立音频流。

## 开始使用

把光标留在目标输入框，F8 开始，停顿后自动识别并尝试输入，最后 F8 结束；F9 取消本轮待处理内容。不要为了结束录音切回工作台，否则焦点保护可能拒绝向原窗口输入。

自动输入会检查目标窗口是否仍在前台，不能保证所有应用、管理员权限窗口都能接收。拒绝输入时，结果仍保留在工作台。自动粘贴模式会更新电脑剪贴板；不读取旧剪贴板。手动整段识别、自动断句两种模式均可选。

低频过滤和语音检测无法分离使用者身份。手机靠近嘴边、远离扬声器通常更有利；必要时使用可选的录音期间静音，结束后恢复。0.8 秒是断句等待，还要加模型识别和排队时间。实际口音、噪声下的准确率需自行测试。
