# BIT-Web 自动重连与登录

这是一个无需第三方依赖的 Windows Python 程序。它每 30 秒检查一次真实外网连通性；不可用时会：

1. 连接已保存的 `BIT-Web` Wi-Fi 配置；
2. 按北京理工大学深澜（SRun）门户的 challenge 加密流程登录；
3. 重新检查网络；若仍不可用，每 10 秒重试，直至恢复。

认证中的 `info` 字段使用北理门户配置的自定义 Base64 字母表；相关值可在 `config.json` 的 `portal.base64_alphabet` 中覆盖。

如果 SSID 仍显示为 `BIT-Web`，但校园网门户连续 2 次中断连接，程序会自动进行分层恢复，并继续认证。该过程只使用 Windows 本地 Wi-Fi 配置、DHCP 和校园网内网，不依赖外网：

1. 确认 Windows 实际已关联到 `BIT-Web`（不是只看连接命令是否提交成功）；
2. 若 Windows 软件无线电被任务栏 Wi-Fi 开关关闭，自动开启无线电，再连接已保存的 `BIT-Web` 配置；
3. 模拟任务栏 Wi-Fi 开关“关→开”，再重新连接已保存的 Wi-Fi 配置；
4. 第二轮 Wi-Fi 恢复起，重新获取 DHCP 地址；
5. 若无线电重置失败，使用最高权限计划任务无弹窗地禁用再启用无线适配器；若硬件或系统仍拒绝，才退回普通断开并重新关联；
6. Wi-Fi 的物理重连采用 30 秒、60 秒、120 秒、最多 300 秒的退避，避免校园网门户故障时反复断网；认证请求本身仍按 10 秒间隔继续。

## 首次使用

请确认 Windows 已至少手动连接过一次 `BIT-Web`，使系统保存了该 Wi-Fi 配置。

先复制 `config.example.json` 为 `config.json`，再配置 `config.json` 里的 `credentials`：

```powershell
Copy-Item .\config.example.json .\config.json
```

账号密码示例：

```json
"credentials": {
  "username": "对方的校园网账号",
  "password": "对方的校园网密码"
}
```

也就是删除原本的：

```json
"password_encrypted": "……"
```

然后在此目录打开 PowerShell：

```powershell
python .\campusnet.py --once
```

首次运行会把 `config.json` 中的明文密码替换为 Windows DPAPI 密文，只能由当前 Windows 用户解密。

## 重新安装

安装或重新安装自动运行，请执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
```

该命令会出现一次 Windows UAC 确认：确认后，程序会以当前用户的“最高权限”计划任务在登录时自动后台运行，且立即替换当前的普通后台实例。这样当 Wi-Fi 无线电重置不足时，程序也可自行重置无线适配器；日常运行不会弹窗。

## 停止自动运行

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall_autostart.ps1
```

运行日志写入 `campusnet.log`，其中不会记录账号或密码。日志按天轮转，并自动删除 30 天前的内容。

日志只记录已经发生的动作：网络恢复时会写明“已清除失败计数”；只有 Wi-Fi 恢复后网络仍不可用时，才会说明后续认证重试和下一次物理恢复所需的条件，不会在网络正常时提示即将断开 Wi-Fi。

## 查看日志：

```powershell
Get-Content G:\D_Lizhuoran\Code\CampusNet\campusnet.log -Encoding UTF8 -Wait
```

## 代码逻辑

程序正常时每 30 秒检测一次；检测失败后按每次尝试的开始时间约每 10 秒重试，直至网络恢复。首次发现断网会记录 Wi-Fi 接口、关联状态、SSID、接入点和信号强度；恢复时会记录本次断网总时长。核心逻辑如下：

```text
检测外网是否真的可访问
        │
        ├─ 正常 → 写入“网络正常”日志，等待下一次检测
        │
        └─ 异常
            │
            ├─ 检查当前 Wi‑Fi 是否为 BIT-Web
            │     └─ 不是 → 若软件无线电关闭则自动开启，再连接 BIT-Web
            │
            ├─ 等待 Windows 确认已实际关联 BIT-Web
            │
            ├─ 多轮失败时续租 DHCP；无线电重置失败时以最高权限重置无线适配器
            │
            ├─ 已连接 BIT-Web 后，向校园网认证服务器请求 challenge
            │
            ├─ 用账号、密码和 challenge 按 SRun 规则生成加密登录参数
            │
            ├─ 调用认证接口登录
            │
            └─ 等待 3 秒，再次检测外网是否恢复；失败则 10 秒后重试
```

“断网”的判断不是只看 Wi‑Fi 是否连接，而是访问两个联网检测地址：

- Microsoft 的 `connecttest.txt`，必须返回预期文字。
- Google 的 `generate_204`，必须返回 HTTP 204。

因此即使电脑还连着 `BIT-Web`、但校园网认证已经过期或网络无外网，程序也会识别为异常并重新登录。

连接 Wi‑Fi 的部分调用 Windows 自带命令：

```text
netsh wlan connect name=BIT-Web
```

这要求 Windows 已经保存了 `BIT-Web` 的 Wi‑Fi 配置。认证成功或失败、连接失败等情况都会写入 `campusnet.log`。

`netsh wlan connect` 使用的是本机已保存的 Wi-Fi 配置，而认证接口位于校园网内网 `10.0.0.55`；两者均不依赖外网可用。

`wifi` 中的恢复参数可以按需调整；通常无需修改：

- `reconnect_after_portal_failures`：连续几次门户传输失败后启动一次 Wi-Fi 物理恢复，默认 2。
- `wifi_reconnect_cooldown_seconds` / `max_wifi_reconnect_cooldown_seconds`：物理恢复的初始与最大退避时间，默认 30 / 300 秒。
- `dhcp_renew_after_wifi_recoveries`：第几次物理恢复起续租 DHCP，默认 2。

首次发现断网时，程序仍使用两个外网探测地址确认；后续恢复重试改用一个最多 2 秒的快速探测，然后直接认证，避免每轮都等待两个外网请求超时。`network_check.retry_timeout_seconds` 可调整这个快速探测的超时，默认 2 秒。

若电脑未连接任何 Wi-Fi，程序会跳过外网探测，立即执行本地 `netsh` 命令连接 `BIT-Web`。若正连接其他且能正常联网的 Wi-Fi，则不会抢占该连接。
