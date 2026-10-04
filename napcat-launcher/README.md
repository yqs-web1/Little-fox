# NapCat 启动脚本包

本目录只包含 **启动脚本与元数据**，不含 NapCat 本体（第三方项目，请从官方渠道获取）。

## 目录内容

| 文件 | 说明 |
|---|---|
| `launcher.bat` | 标准启动器 |
| `launcher-win10.bat` | Windows 10 专用启动器 |
| `launcher-user.bat` | 当前用户权限启动器 |
| `launcher-win10-user.bat` | Win10 + 用户权限启动器 |
| `KillQQ.bat` | 结束 QQ 进程 |
| `quickLoginExample.bat` | 快速登录示例 |
| `package.json` / `qqnt.json` | NapCat 元数据（供启动器读取） |
| `loadNapCat.js` | 加载入口 |

## 使用方式

1. 从 [NapCat 官方仓库](https://github.com/NapNeko/NapCatQQ) 获取 NapCat.Shell 发行包，解压到本目录。
2. 按需修改 `config/` 下的配置文件（该目录含账号信息，**不在本仓库中**）。
3. 双击对应的 `launcher*.bat` 启动。

## 隐私说明

`config/` 目录包含 QQ 号、WebUI token 等账号凭据，已被 `.gitignore` 排除，切勿提交。
