# qq-chat-export-macos

**macOS 一键导出本机 QQ / NTQQ 聊天记录为 TXT / JSON / CSV**

macOS arm64 · SQLCipher · lldb · 全程本地处理 · 不上传任何数据

本工具解决一个具体问题：macOS 上 NTQQ 的聊天记录保存在加密的 `nt_msg.db` 里，想导出某个聊天只能靠截图或手动复制。本脚本自动完成「取密钥 → 解密 → 导出」全过程，全程本地运行，导出完成后自动清理所有敏感中间产物。

## 特性

- 一键导出**指定**的群聊或私聊（只导你点名的那个，不做全量铺开）
- 输出 `txt`（可读文本）/ `json`（结构化）/ `csv`（表格）+ 摘要 + 每日统计
- 不修改原版 QQ.app（用 APFS 克隆副本重签），不需要 sudo，不需要关闭 SIP
- 自动清理：密钥、明文数据库、重签副本在导出后全部删除，数据舞台精确还原
- 支持断点续聊场景：密钥每次现取现弃，不落盘保存

## 原理

```
/Applications/QQ.app ──APFS 克隆+ad-hoc 重签(加 get-task-allow)──▶ /tmp 副本
        │                                                          │ lldb 启动
        │                                              断点 PKCS5_PBKDF2_HMAC
        │                                              （QQ 自带 libcrypto.dylib）
        ▼                                                              │
真实数据舞台（~/Library/Application Support/QQ/）                        ▼
  auth 拷贝 + nt_db 克隆 + nt_data 软链              抓到 16 位数据库密钥
        │                                                              │
        ▼                                                              ▼
剥 1024 字节 QQ_NT 头 ◀── sqlcipher（4096页/4000迭代/HMAC_SHA1/PBKDF2_SHA512）
        │
        ▼
明文 SQLite ──▶ export_ntqq_chat.py ──▶ *_full.txt / .json / .csv
```

密钥提取思路来自公开研究（见文末致谢）；本仓库的 `/tmp` 克隆重签、非容器路径数据舞台、按 salt 匹配密钥的解析器与一键自动化为原创实现。

## 依赖

```bash
brew install sqlcipher
xcode-select --install   # 提供 lldb，多数开发机已有
```

- macOS Apple Silicon（arm64），已登录过的 NTQQ
- Python 3.9+（系统自带即可，无第三方依赖）

## 用法

```bash
git clone https://github.com/XKCCC/qq-chat-export-macos.git
cd qq-chat-export-macos

# 1. 完全退出 QQ（Cmd+Q）
# 2. 运行（以好友 QQ 号为例；群聊直接给群号）
python3 macos_extract_and_export.py --peer 123456789 --self-uin 你的QQ号
```

过程中：

1. 会弹出一个重签副本的 QQ 窗口 —— 如果它自动登录了就什么都不用做；没有的话用手机 QQ 扫码登录一次
2. 等聊天列表加载出来，脚本抓到密钥后会自动完成剩下的事并关掉副本
3. 完成后重新打开你的原版 QQ 即可（原版从未被改动）

参数：

| 参数 | 说明 |
|---|---|
| `--peer` | 必填。群号 / 好友 QQ 号 / nt_uid，群或私聊自动判断 |
| `--self-uin` | 你的 QQ 号（也可用环境变量 `QQ_SELF_UIN`），用于标记哪些消息是你发的 |
| `--out-dir` | 输出目录，默认 `~/qq-chat-export` |
| `--timeout` | 等密钥的超时秒数，默认 600 |
| `--keep` | 保留临时工作目录（**含敏感数据**，调试用，用完自己删） |

## 输出

默认输出到 `~/qq-chat-export/`：

| 文件 | 用途 |
|---|---|
| `<前缀>_full.txt` | 按时间排序的完整对话文本，直接阅读 |
| `<前缀>_full.json` | 结构化消息，适合脚本/agent 处理 |
| `<前缀>_full.csv` | 表格，Numbers/Excel 直接打开 |
| `<前缀>_summary.txt` | 消息数、时间范围、双方发言统计 |
| `<前缀>_daily_counts.csv` | 每日消息量 |

## 常见问题

| 问题 | 回答 |
|---|---|
| 会动我的原版 QQ 吗 | 不会。所有操作都在 /tmp 的克隆副本上，原版签名从未改动 |
| 数据会上传吗 | 不会。全程本地，导出后密钥/明文库/副本全部自动删除 |
| 为什么要扫码 | 重签后副本访问不了钥匙串里的登录态；若容器里的 login.enc 仍有效则自动登录，无需扫码 |
| 找不到某个聊天 | 该聊天在本机没有本地消息。先用原版 QQ 打开它加载历史，再重跑 |
| QQ 更新后失效 | 可能。加密参数或符号变了的话需要适配，欢迎提 issue |
| 支持 Intel Mac / Windows 吗 | 不支持。思路可参考文末致谢里的项目 |

## 安全与隐私

- 本工具只能导出**本机已登录账号**的聊天记录，相当于把你自己的数据读出来
- 密钥只在内存和 lldb 日志里短暂存在，导出结束即随工作目录一起删除
- 请勿用于导出他人账号数据；请勿把导出的聊天记录上传到不受信的位置

## 致谢

| 项目 | 用途 |
|---|---|
| [zywstudy123/qq-bak-chat-history-export](https://github.com/zywstudy123/qq-bak-chat-history-export) | `export_ntqq_chat.py`（MIT）与 `create_synthetic_ntqq_fixture.py` 直接来自该仓库 |
| [QQBackup/nt_msg_db_util](https://github.com/QQBackup/nt_msg_db_util) | SQLCipher 参数（页大小/迭代/HMAC 组合） |
| [Mythologyli/qq-nt-db](https://github.com/Mythologyli/qq-nt-db) | 1024 字节头部与 `sqlite3_key_v2` 思路 |
| [dxfxbfg/qq-cli](https://github.com/dxfxbfg/qq-cli) | `PKCS5_PBKDF2_HMAC` 挂钩取密钥思路 |
| [jielosc/qqnt-export-macos](https://github.com/jielosc/qqnt-export-macos) | Apple Silicon 重签 + lldb 路线思路 |

## 免责声明

本项目仅用于导出和整理使用者有权访问的本机聊天记录。请遵守当地法律法规、平台服务条款和数据授权边界。本项目不隶属于腾讯，不对任何账号、数据或法律后果承担责任。

## License

[MIT](LICENSE) © 2026 XKCCC
（`export_ntqq_chat.py` 与 `create_synthetic_ntqq_fixture.py` 为 MIT 协议转载，原作者见致谢）
