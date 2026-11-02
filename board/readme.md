# 板端控制与适配

## 通用控制入口

控制端只需要 Python 和 OpenSSH，可在 Windows、Linux 或 macOS 上运行。计算与显示在 KV260 上执行。

```bash
python3 board/control.py start --host 192.168.1.100
python3 board/control.py check --host 192.168.1.100
python3 board/control.py status --host 192.168.1.100
python3 board/control.py stop --host 192.168.1.100
```

默认安装目录为远端用户的 ~/twincoach，~ 在远端展开。--user、--port 和 --root 可覆盖用户、端口和目录；也可使用 TWINCOACH_HOST、TWINCOACH_USER、TWINCOACH_ROOT 环境变量。

## 配置文件

复制 config.example.json 为 config.json，再填入实际设备信息：

```bash
python3 board/control.py start --config board/config.json --dry-run
python3 board/control.py start --config board/config.json
```

命令行参数优先于配置文件。用户配置不进入 Git。

| 字段 | 作用 |
| --- | --- |
| host、user、port | SSH 目标 |
| root | 远端安装目录，支持 ~ 和空格 |
| front、side | 正面和侧面摄像头设备，推荐使用稳定的 /dev/v4l/by-id/ 路径 |
| front_rotation、side_rotation | 图像旋转，支持 0/90/180/270 度 |
| display | 远端显示会话，如 :0 |
| xauthority | 显示授权文件；为空时使用远端 XAUTHORITY 或该用户的 GDM 路径 |
| mode | full33 或 hybrid |
| profile | 相对安装目录的配置文件，或绝对路径 |
| swap_views | 显式交换评分视角映射 |

正面与侧面按实际物理视角配置，显示位置与评分映射分别处理。更换相机或摆放位置时，应同时调整旋转、内外参和角色绑定。

## Windows 入口

```powershell
.\board\start.ps1 -Config '.\board\config.json' -DryRun
.\board\start.ps1 -Config '.\board\config.json'
.\board\test.ps1 -Config '.\board\config.json'
.\board\stop.ps1 -Config '.\board\config.json'
```

-BoardIP、-UserName、-Port、-BoardRoot、-Front、-Side 和 -XAuthority 可覆盖对应配置。-LocalOnly 检查本地源码目录与 SSH 命令，不连接板卡。

## 运行环境

通过 build/stage.py 装配源码，再提供模型、原生库、驱动、固件、角色和标定。应用启停检查匹配驱动，不自动替换位流。基础系统需具备 VART/XRT、图形环境和与内核匹配的模块。

显式恢复命令使用 restore --command-file 指定配置文件；不依赖开发者机器上的固定路径。设备与驱动接口名在代码中保持一致，相关资产见 [资产说明](../report/assets.md)。
