# ESP32 + StreamShot

让现有 AI 客户端按需看见 iPhone 屏幕，再通过 ESP32-S3 蓝牙键鼠操作手机。

手机用 StreamShot 共享屏幕，电脑运行随 MCP 启动的本地桥，ESP32 通过 USB 接命令、蓝牙发输入。无需新增模型 API Key、云服务或独立连接助手。

**当前是开发预览。** 需要含 AI Link 的 StreamShot 开发版；商店 1.0 尚未包含此能力，不要为了它购买旧版。本仓包含 skill、本地桥和原型固件，不包含 iOS App 源码或公开安装包。

## 安装

把这句话交给 Codex：

```text
使用 $skill-installer 安装 https://github.com/cctanfujun/esp32-streamshot/tree/main/skills/esp32-streamshot
```

再说：

```text
使用 $esp32-streamshot 连接我的 iPhone，先在空白备忘录验证键盘和鼠标。
```

首次配置见[安装与连接](skills/esp32-streamshot/references/setup.md)。手机与电脑同网，AI Link 点选电脑、首次确认信任，再点广播图标亲自确认 Start Broadcast。无需电脑配对码、扫码或查找连接文件。蓝牙配对与系统辅助触控另按设备设置。

skill 复用连接，用 `streamshot_act` 将一小段已确定输入与后续取图合为一次调用。输入未知和截图超时分开处理，避免重复输入；新页面仍先观察，不能盲跑长脚本。真实设备已确认鼠标移动、备忘录输入、系统搜索、打开微信和切到聊天页；约 25 秒的电脑服务中断后自动续屏/新图读取已通过。完整微信发送、任意 App、长时间稳定性尚未验收，取帧延迟和流畅控制仍在优化。

目前是相对鼠标与 HID 按键，不是绝对触屏或任意中文注入。图像进入你选用的 AI 客户端后，处理和留存按该服务规则。固件见 [firmware](firmware/README.md)，许可证 Apache-2.0。反馈时勿公开屏幕、配对文件或密钥。
