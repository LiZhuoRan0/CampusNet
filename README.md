# BIT-Web 自动重连与登录

这是一个无需第三方依赖的 Windows Python 程序。它每 60 秒检查一次真实外网连通性；不可用时会：

1. 连接已保存的 `BIT-Web` Wi-Fi 配置；
2. 按北京理工大学深澜（SRun）门户的 challenge 加密流程登录；
3. 重新检查网络；若仍不可用，每 10 秒重试，直至恢复。

认证中的 `info` 字段使用北理门户配置的自定义 Base64 字母表；相关值可在 `config.json` 的 `portal.base64_alphabet` 中覆盖。

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

首次运行会把 `config.json` 中的明文密码替换为 Windows DPAPI 密文，只能由当前 Windows 用户解密。程序已安装到当前 Windows 用户的“启动”文件夹，登录后会自动运行。

## 重新安装

若日后需要重新安装，可执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
```

## 停止自动运行

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall_autostart.ps1
```

运行日志写入 `campusnet.log`，其中不会记录账号或密码。日志按天轮转，并自动删除 30 天前的内容。

## 查看日志：

```powershell
Get-Content G:\D_Lizhuoran\Code\CampusNet\campusnet.log -Encoding UTF8 -Wait
```

## 代码逻辑

程序正常时每 60 秒检测一次；检测失败后按每次尝试的开始时间约每 10 秒重试，直至网络恢复。核心逻辑如下：

```text
检测外网是否真的可访问
        │
        ├─ 正常 → 写入“网络正常”日志，等待下一次检测
        │
        └─ 异常
            │
            ├─ 检查当前 Wi‑Fi 是否为 BIT-Web
            │     └─ 不是 → 执行 Windows 的 netsh 命令连接 BIT-Web
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

若电脑未连接任何 Wi-Fi，程序会跳过外网探测，立即执行本地 `netsh` 命令连接 `BIT-Web`。若正连接其他且能正常联网的 Wi-Fi，则不会抢占该连接。
