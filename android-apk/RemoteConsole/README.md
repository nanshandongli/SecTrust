# 协作审计台 · 手机远程控制台（Android APK 工程）

这是一个**极简的 Android WebView 外壳**：它把电脑端「协作审计台」自带的手机网页控制台
（`http://<电脑IP>:<端口>/mobile`）加载进一个 App，从而把手机变成一个遥控器。

> 真正的控制逻辑（登录、对话、任务、审批、助理通道等）全部在电脑端产品里，本 App 不做任何业务判断。

---

## 一、开发环境（只需装一次）

- **Android Studio**（最新版，内置 Gradle 与 SDK 管理器）
- 首次同步会提示安装 **Android SDK Platform 34** 与 **JDK 17**（Studio 会自动引导）

> 本仓库**不附带可安装的 `.apk` 二进制**：当前开发环境没有 Android SDK 工具链，无法在此编译。
> 你在自己的电脑上用 Android Studio 打开本项目即可一键编译/运行。

## 二、打开并编译

1. 启动 Android Studio → `File → Open` → 选择本目录 `android-apk/RemoteConsole`。
2. 等待 Gradle 同步完成（首次会下载 Gradle 8.2 与 AGP 8.2.0，需联网）。
3. 连接安卓手机（开启「开发者选项 → USB 调试」），点击 ▶ Run，选你的设备即可安装运行。
   - 或 `Build → Build Bundle(s) / APK(s) → Build APK(s)` 生成可安装的 `app-debug.apk`。

> 若 Android Studio 提示缺少 Gradle Wrapper，点「Fix」让它自动生成 `gradle-wrapper.jar` 即可
> （本仓库已提供 `gradle-wrapper.properties`，只缺二进制 jar，Studio 会补齐）。

## 三、手机上怎么用

1. **电脑端先启动可被手机访问的服务**（见第四节），确认手机和电脑在同一 Wi-Fi / 局域网。
2. 打开本 App，首次会弹出「服务器地址」输入框。
3. 填入电脑端地址（**只需填到端口，App 会自动补 `/mobile`**），例如：
   ```
   http://192.168.1.50:8088
   ```
4. 点「连接」→ 进入手机控制台：登录（任意用户名 + 角色）→ 底部五个标签：
   **概览 / 对话 / 任务 / 审批 / 助理**。
5. 顶栏「地址」按钮可随时更换服务器；返回键先在网页内后退，再退出 App。

## 四、电脑端怎么启动（让手机能访问）

产品控制台默认只绑 `127.0.0.1`（仅本机）。要让手机访问，需绑到 `0.0.0.0`：

```bash
cd 项目根目录
python console_web.py --host 0.0.0.0 --port 8088
```

- 查看电脑局域网 IP：`ipconfig`（Windows）找「IPv4 地址」（通常是 `192.168.x.x` 或 `10.x.x.x`）。
- **安全提示**：`0.0.0.0` 会把服务暴露给整个局域网。请只在**可信的 Wi-Fi** 下使用；
  离开时 `Ctrl+C` 关闭。产品本身有 JWT 登录（无密码校验，等同内网信任），不要在公网暴露。

## 五、实现要点

| 文件 | 作用 |
|------|------|
| `app/src/main/java/com/example/remoteconsole/MainActivity.java` | WebView 外壳，保存/输入服务器地址，处理返回键 |
| `app/src/main/res/layout/activity_main.xml` | 顶栏（标题 + 地址按钮）+ 全屏 WebView |
| `app/src/main/AndroidManifest.xml` | `INTERNET` 权限 + `usesCleartextTraffic` + 网络安全配置 |
| `app/src/main/res/xml/network_security_config.xml` | 允许局域网 HTTP 明文（方便输入任意电脑 IP） |
| `mobile_console.html`（项目根目录，由电脑端 `/mobile` 提供） | 手机优先 SPA：登录 + 概览/对话/任务/审批/助理 |

## 六、已知边界（诚实说明）

- 本 App 仅用系统 WebView，**不依赖任何第三方 SDK**。
- `network_security_config.xml` 对**所有域名**开放了 cleartext（HTTP），以便输入任意局域网 IP；
  如果你只走 HTTPS，可改为仅允许特定域名。
- APK 需在你本机 Android Studio 编译；本仓库交付的是**可直接打开编译的工程源码**。
- 手机「对话」功能依赖电脑端已配置并启动的算力模型（如本地 Ollama）；未配置时对话会诚实返回
  「无可用 LLM 后端」，不会伪造回答。
