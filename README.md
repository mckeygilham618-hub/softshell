# Softshell 软壳

**给你付费订阅的 Claude 一个聊天软件风格的沟通界面。**

**A chat-app style interface for the Claude subscription you already pay for.**
Runs on the Claude Code CLI. No Claude desktop app, no admin rights, no virtualization.

> **EN TL;DR** — Softshell is a local, WeChat-style chat window for the Claude Code CLI on Windows: chat bubbles, stickers, avatars, multi-session, and group chats where several Claude models talk to each other. One Python file + one HTML file, no server, no telemetry, MIT. UI is Chinese-first.

---

![Softshell 截图](softshell.png)

## 这是什么

一个在你自己电脑上跑的聊天窗口。你打字，它调用你本机的 Claude Code CLI，回复以聊天气泡的形式显示出来。**又能聊天，又能直接干活。**

- **界面全中文**，像用聊天软件一样
- **消息双向渲染**：你发的消息也享受 markdown 排版——粗体、斜体、删除线、表格、代码块、列表、引用、链接都认
- **能发图**：点回形针选图，或直接 `Ctrl+V` 粘贴截图
- **表情包**：往 `stickers/` 里丢图，AI 会自己挑合适的发给你；找不到合适的，它会开口跟你要
- **随时打断**：运行中点 ■ 或按 `Esc`，立刻停下，不再消耗额度
- **多会话＋群聊**：左侧栏开多个会话，每个配不同的模型和形象；还能把几个 Claude 拉进一个群，@谁谁发言——三个模型当场辩论，一次 API 都不用切
- **聊天记录留在本地**：每个会话自动留档，重开窗口不丢；能搜索、能置顶、能导出 md
- **记忆归你管**：跟 Claude Code CLI 共用同一份本地记忆文件，你能看、能改、能删；会话记录、表情包也全在你自己硬盘上

## 它不需要什么

- 不需要装 Claude 桌面应用
- 不需要管理员权限
- 不需要开启 Virtual Machine Platform 或 Hyper-V
- 不需要特定订阅档位——只要你的 `claude` 命令能跑就行（订阅、API key、云厂商、网关都算）
- 不需要联网到本工具的任何服务器（因为没有）

## ⚠️ 装之前先知道：本产品移除了权限刹车

Claude 桌面版在任务进行中会要求授权各种操作权限。**Softshell 移除了这道刹车。**

原因很实际：它问得太频繁，没人回答它就自己停在那里不接着干。

所以在 Softshell 里，Claude 会直接在你机器上跑各种命令，不再问你。运行中点 ■ 或按 `Esc` 可以随时打断。

如果你是每次Claude问你要授权，你100%允许的，你适合本产品；如果你要认真审批它的请求的，不要下载此产品。

除了运行中随时可按的停止键（■ 或 Esc），软壳没有安装任何其他刹车。

（带权限确认的版本在计划中。）

## 安装

**前置条件：**[Python 3.8+](https://www.python.org/downloads/)（安装时勾选 *Add to PATH*）和 [Claude Code CLI](https://code.claude.com/docs/en/quickstart)，并且 `claude` 命令能正常登录使用。

**免费 Claude 账户用不了本产品**——这是 Claude Code CLI 本身的限制，不是 Softshell 的。你需要 Pro / Max / Team / Enterprise 订阅，或者 Claude Console 的 API 额度。

Softshell **不捆绑 Claude Code CLI**，你需要自己安装它、用自己的账号登录。

```bash
git clone https://github.com/mckeygilham618-hub/softshell.git
cd softshell
python bridge.py
```

跑起来会自动弹出聊天窗口。之后想再开，双击 `bridge.py` 即可，也可以自己建个快捷方式放桌面。

> Windows 上如果不想每次弹出黑色控制台窗口，用 `pythonw.exe bridge.py` 代替 `python bridge.py`。

## 用法

**表情包**：`stickers/` 下面每个子文件夹是面板里的一个页签，文件夹名就是页签名。往里丢图片就行，不用重启，关掉面板再点开就有了。

**文件名就是标签** —— AI 靠文件名判断该发哪张，所以名字起得越清楚越准。建议用「情绪-动作」的写法：

```
stickers/共享/开心-举手欢呼.png
stickers/共享/无奈-扶额.jpg
```

嫌一张张改名麻烦？可以让 Claude 自己读图重命名——这个工具本身就能干这活。

**头像和聊天背景**：默认是白方块（Claude）、绿方块（你）、素色背景。想换，把图丢进表情包文件夹，然后直接跟 Claude 说「把你的头像换成XX」「把聊天背景换成XX」——它自己会弄，你也可以让它把自己的头像挑了。顺便，表情包批量改名也可以一起叫它干。

**多会话**：左侧栏「＋新会话」开新的，每个会话独立记忆、独立头像背景、独立模型设置——你可以给不同的会话配不同的 Claude 形象。右键会话可以改名、置顶、导出聊天记录、删除。

**群聊**：侧栏「👥 群」，勾选至少两个会话拉群。打 `@` 会弹出成员菜单，@谁谁发言，可以@多个人——按@的先后顺序轮流开口，后说话的能看到前面刚说了什么，正好用来让不同模型当场辩论。不@人的消息会记进群里，成员下次发言时能看到。群成员借用单聊联系人的名字、头像和模型设置，但**群里的记忆和单聊是分开的**（模型的会话链没法两边共用，这是架构限制，不是 bug）。

**查找与导出**：标题栏 🔍 搜当前会话的聊天记录，分全文／图片与表情包／链接／文件路径四类，点结果跳回原消息位置。聊天记录自动留档在本地 `history/` 和 `groups/` 文件夹（永不上传），重开窗口自动恢复；右键会话可导出成 md 文件，落在 `exports/`。

## 语音通话（可选件）

微信语音通话式的界面：点📞进入通话页（大头像＋状态＋挂断键，通话中不显示文字），开口即说，说完停顿即发送；回复**边生成边逐句朗读**，不等全文；**它讲话时你插一句，它就闭嘴听你的**。挂断后整段通话变成文字气泡留在聊天窗里，接着打字聊记忆无缝衔接。

采音在你的浏览器内完成（自带回声消除，外放也不会自听自话）；识别在你本机完成，**完全离线、免费、无密钥**，中文准确率优于 Whisper-large-v3。回复速度取决于所选模型的思考时长——快模型首句几秒可达，深思考模型该想还是要想。

不装不影响软壳本体。想用的话三步：

1. 安装依赖：

```bash
pip install sherpa-onnx
```

2. 下载两个模型文件，放进软壳目录的 `voice\` 文件夹：
   - [SenseVoice 识别模型](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2)（约250MB，解压后把里面的 `model.int8.onnx` 和 `tokens.txt` 放进 `voice\`）
   - [silero_vad.onnx](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx)（约2MB，断句用）
3. 点📞开聊（首次会请求麦克风权限）。

**热词纠正**：语音识别爱按词频抢答（比如把 Claude 听成 cloud）。第一次通话后 `voice\hotwords.txt` 会自动出现，每行一条「听错的词=>该是的词」，自己往里加就行，存盘即生效。

**朗读嗓音**在底部状态栏「嗓音」里选，点击即试听，**每个会话可以绑定不同的嗓音**——给不同的 Claude 形象配不同的声线。本地男女声开箱即用；想要更好听的**在线神经嗓音**（晓晓、云希、东北小北等8款），再装一个：

```bash
pip install edge-tts
```

在线嗓音走微软免费接口（需联网，无需密钥）；断网或没装时自动降级回本地嗓音，通话不中断。

> 语音识别引擎为阿里 FunAudioLLM 团队开源的 SenseVoice-Small（FunASR Model License v1.1，允许商用、要求署名——特此致谢）。支持普通话、粤语、英语、日语、韩语。
> 说明：回复速度取决于所选 Claude 模型的思考时长，体验更像对讲机而非电话——它听得快，想得没那么快。

## 合规声明

- 本工具使用**你自己的** Claude 账号与额度。不代理、不中转、不代付，不收集或存储任何账号凭证。
- 不修改 Claude Code 的任何文件，只是调用它。
- 不绕过任何地区限制或使用条款。
- 所有数据只在你本机与 Anthropic 官方服务之间流动，本工具不上传任何东西到任何服务器。

## 免责

本项目与 Anthropic 无隶属、赞助或背书关系。Claude 和 Claude Code 是 Anthropic, PBC 的商标。

This project is not affiliated with, sponsored by, or endorsed by Anthropic. Claude and Claude Code are trademarks of Anthropic, PBC.

## 许可证

[MIT](LICENSE) © 2026 YAN YAN

## 致谢

本项目由 YAN YAN 设计与主导，代码在 Anthropic 的 Claude 协助下完成（Fable 5、Opus 5）。人机分工见 [AI_USAGE.md](AI_USAGE.md)。
