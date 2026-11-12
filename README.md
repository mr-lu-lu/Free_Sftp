# FTP Mate

FTP Mate 是一个面向 Windows 的轻量双栏文件传输客户端，支持 **SFTP、FTP 和显式 FTPS**。项目基于 Python 和 Tkinter 开发，可用于连接 Linux 服务器进行文件上传、下载和目录管理。

## 功能

- 左侧本机、右侧远程服务器的双栏文件管理
- 双击文件夹进入，双击文件直接上传或下载
- 左右两侧文件列表双向拖拽上传/下载
- 支持从 Windows 资源管理器拖拽文件或文件夹到远程目录
- 支持多选、目录递归上传和递归下载
- 文件传输进度、速度、取消、覆盖/跳过/终止确认
- 上传断点续传，下载临时文件保护
- SFTP 支持密码和私钥登录，默认端口 22
- SFTP 首次连接记录 SSH 主机密钥，后续密钥变化会拒绝连接
- FTP/FTPS 支持被动模式，显式 FTPS 支持 TLS 证书校验
- 保存连接配置，密码和私钥口令通过 Windows DPAPI 加密保存
- 自动记住本机目录和每个已保存连接最后进入的远程目录
- SFTP 自动识别 UTF-8、GB18030、GBK 文件名编码，也支持手动指定
- 内置传输日志、主题切换和 F5 刷新

## 环境要求

### 运行源码

- Windows 10 或 Windows 11，64 位
- Python 3.11 推荐，最低建议 Python 3.10
- 可访问目标 FTP/FTPS/SFTP 服务器的网络

### 构建可执行文件

- 上述 Windows 和 Python 环境
- PyInstaller 6.x
- 项目提供的 `requirements-dev.txt` 中包含构建和测试依赖

> 目标电脑运行打包后的 exe 时，不需要安装 Python、pip 或本项目依赖。

## 源码运行

### 1. 克隆仓库

```powershell
git clone https://github.com/mr-lu-lu/Free_Sftp.git
cd Free_Sftp
```

### 2. 创建虚拟环境

推荐使用 Python 3.11：

```powershell
py -3.11 -m venv .venv
```

如果系统中只有一个 Python 版本，也可以使用：

```powershell
python -m venv .venv
```

### 3. 安装依赖

仅运行程序时使用运行时依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果要同时运行测试或打包 exe，安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` 已包含 `requirements.txt`，因此开发环境只需执行一次即可。

### 4. 启动程序

```powershell
.\.venv\Scripts\python.exe main.py
```

也可以双击 `run.bat`。`run.bat` 会优先使用 `.venv\Scripts\pythonw.exe` 启动，不显示控制台窗口。

## 编译为 Windows exe

项目使用 PyInstaller 的 `--onefile --windowed` 模式打包，最终生成单个无控制台窗口的可执行文件。

### 一键构建

PowerShell：

```powershell
.\build.ps1
```

命令提示符：

```bat
build.bat
```

构建脚本会执行以下操作：

1. 检查 `.venv\Scripts\python.exe` 是否存在
2. 安装 `requirements-dev.txt`
3. 清理旧构建缓存
4. 使用 PyInstaller 生成单文件 exe

构建结果：

```text
dist\FTPMate.exe
```

该 exe 可在普通的 Windows 10/11 64 位电脑上独立运行，无需安装 Python。

### 手工构建

如果不使用构建脚本，可以执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name FTPMate --collect-all sv_ttk --collect-all tkinterdnd2 --collect-all paramiko main.py
```

构建参数说明：

- `--onefile`：生成单个 exe，启动时解压到临时目录
- `--windowed`：GUI 程序不显示控制台窗口
- `--collect-all sv_ttk`：打包主题资源
- `--collect-all tkinterdnd2`：打包 tkdnd DLL 和拖拽组件
- `--collect-all paramiko`：打包 SSH/SFTP 依赖

> 不建议省略 `--collect-all tkinterdnd2`，否则打包后的拖拽功能可能不可用。

## 使用说明

1. 选择协议：Linux 服务器推荐 `SFTP`；普通 FTP 选择 `FTP`；需要 TLS 时选择 `FTPS (显式)`。
2. 填写主机、端口、用户名和密码。SFTP 默认端口为 22，FTP/FTPS 默认端口为 21。
3. SFTP 可在“高级”中选择私钥文件并填写私钥口令。
4. 点击“连接”，右侧显示远程目录。
5. 左侧文件拖到右侧可上传；右侧文件拖到左侧可下载。
6. 也可以从 Windows 资源管理器把文件拖到右侧远程区域上传。
7. 双击文件夹进入目录，双击文件执行上传或下载。
8. 连接配置可以保存，下次从下拉框直接选择。
9. 本机目录和每个已保存连接的最后远程目录会自动记录。

### 快捷键

| 快捷键 | 功能 |
| --- | --- |
| `F5` | 刷新本机和远程目录 |
| `Ctrl+U` | 上传左侧选中的文件 |
| `Ctrl+D` | 下载右侧选中的文件 |
| `Ctrl+Enter` | 使用当前表单信息重新连接 |
| `Ctrl+A` | 全选当前文件列表 |
| `Delete` | 删除右侧选中的远程项目 |
| `F2` | 重命名右侧选中的远程项目 |

## 配置与数据目录

程序不会把密码明文写入配置文件。配置文件位于：

```text
%APPDATA%\FTPMate\settings.json
```

SFTP 主机密钥记录位于：

```text
%APPDATA%\FTPMate\known_hosts
```

密码和私钥口令通过 Windows DPAPI 加密，只能由当前 Windows 用户解密。复制 exe 到其他电脑不会自动带走这些配置。

## 运行测试

安装开发依赖后执行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

界面冒烟测试会自动启动本地 FTP/SFTP 测试服务器：

```powershell
.\.venv\Scripts\python.exe tests\ui_smoke.py
.\.venv\Scripts\python.exe tests\ui_sftp_smoke.py
```

测试使用临时目录和临时 `APPDATA`，不会修改真实的 FTP Mate 配置。

## 项目结构

```text
Free_Sftp/
├─ ftpmate/
│  ├─ core.py          # FTP/FTPS 客户端、路径与传输辅助逻辑
│  ├─ sftp.py          # SFTP 客户端、编码识别和主机密钥校验
│  ├─ profiles.py      # 配置持久化、DPAPI 密码保护
│  ├─ ui.py            # Tkinter 界面、双向拖拽和传输调度
│  └─ __init__.py
├─ tests/
│  ├─ test_core.py
│  ├─ test_sftp.py
│  ├─ ui_smoke.py
│  └─ ui_sftp_smoke.py
├─ main.py             # 程序入口
├─ requirements.txt    # 运行时依赖
├─ requirements-dev.txt
├─ build.ps1           # PowerShell 构建脚本
├─ build.bat           # CMD 构建脚本
├─ run.bat             # 源码启动脚本
└─ README.md
```

## 常见问题

### 中文文件名乱码

FTP/FTPS 可在“高级 → 远程编码”中设置编码。SFTP 会自动尝试 UTF-8、GB18030、GBK；如果仍然乱码，可手动选择 `gb18030`。

### 拖拽不可用

- 请使用本项目最新源码或最新构建的 exe
- 构建时必须包含 `--collect-all tkinterdnd2`
- 左侧到右侧是上传，右侧到左侧是下载

### SFTP 主机密钥变化

服务器重装或更换 SSH 密钥后，程序会拒绝连接。确认服务器指纹正确后，删除 `%APPDATA%\FTPMate\known_hosts` 中对应记录并重新连接。

### 连接 FTP 成功但目录为空或超时

检查服务器被动模式端口和防火墙设置。显式 FTPS 需要服务器支持 TLS；自签名证书仅建议在可信内网中临时关闭证书校验。

### Windows SmartScreen 提示

当前构建没有代码签名，Windows 可能提示“未知发布者”。这不代表缺少运行依赖。对外分发时建议使用可信代码签名证书签名 exe。

## 安全说明

- 普通 FTP 不加密用户名、密码和文件内容，公网环境建议使用 SFTP、FTPS 或 VPN。
- SFTP 使用 SSH 主机密钥校验，首次连接应核对目标服务器指纹。
- 保存的密码通过 Windows DPAPI 加密，但程序仍运行在当前用户权限下，请保护好 Windows 账户。
- 发布或分享代码时不要提交本地 `%APPDATA%\FTPMate` 配置、私钥、日志或已编译 exe。

## 构建产物与版本控制

仓库只提交源码、测试和构建脚本。以下内容已由 `.gitignore` 排除：

- `.venv/`
- `build/`
- `dist/`
- `*.spec`
- `*.exe`
- Python 缓存、测试缓存和本地 IDE 文件

因此克隆仓库后需要按本文档重新安装依赖、运行源码或构建 exe。
