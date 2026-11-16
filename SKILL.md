---
name: qq-chat-export-macos
description: Export QQ / NTQQ chat history on macOS (Apple Silicon) to agent-readable TXT, JSON, and CSV. Use when the task involves QQ聊天记录导出, QQ聊天记录转txt/json/csv, NTQQ nt_msg.db inspection or decryption, local chat search, summarization, or preparation for AI agent analysis on a Mac.
---

# QQ Chat Export (macOS)

## Overview

One-shot local pipeline: extract the NTQQ SQLCipher key from a re-signed QQ
copy via lldb, decrypt `nt_msg.db` / `group_info.db` / `profile_info.db`,
and export ONE user-named chat to `txt/json/csv`. Everything runs locally;
no chat data or keys leave the machine.

## Usage

```bash
# 依赖：brew install sqlcipher；Xcode CLT（lldb）
# 前置：完全退出原版 QQ
python3 macos_extract_and_export.py --peer <群号|好友QQ号|nt_uid> --self-uin <自己的QQ号>
```

- `--chat-type group|c2c` 默认自动判断（先查群列表，再查好友资料）
- `--out-dir` 默认 `~/qq-chat-export`
- `--timeout` 抓密钥超时秒数，默认 600
- `--keep` 保留临时工作目录（含敏感数据，调试用）

过程中用户可能需要：退出原版 QQ（脚本自检）、在弹出的重签副本里扫码登录一次
（若容器内 login.enc 仍有效则自动登录）、结束后重新打开原版 QQ。

## How It Works (排障要点)

1. `cp -Rc` 克隆 `/Applications/QQ.app` 到 `/tmp/qq_export_<ts>/` 并 ad-hoc 重签
   （去 app-sandbox、加 get-task-allow）。原版 app 不受影响，无需 sudo、无需关 SIP。
2. 在 `~/Library/Application Support/QQ/` 搭数据舞台：拷贝容器 `auth/`、克隆
   `nt_qq_<hash>/nt_db`、软链 `nt_data`。**关键**：去掉沙箱后 QQ 解析到非容器路径，
   不搭舞台会新建一套空库导致抓不到真密钥。清理按进入前快照精确还原。
3. lldb 断点 `PKCS5_PBKDF2_HMAC`（`--shlib libcrypto.dylib`）：x0=passphrase、
   w1=长度、x2=salt、w4=迭代数（=4000）。用各库文件（剥 1024 字节 `QQ_NT DB`
   头后）的前 16 字节 salt 把断点命中匹配到具体库。
4. sqlcipher 参数：`cipher_page_size=4096`、`kdf_iter=4000`、
   `cipher_hmac_algorithm=HMAC_SHA1`、`cipher_kdf_algorithm=PBKDF2_HMAC_SHA512`，
   `sqlcipher_export` 出明文库。
5. 调 `export_ntqq_chat.py` 导出指定聊天（`*_full.txt/.json/.csv` + summary +
   daily_counts）。
6. 清理：按 /tmp 路径精确 kill 副本（绝不碰原版）、还原舞台、删除工作目录
   （密钥、明文库、lldb 日志一并删除）。

## 纪律

- 只导出用户点名的那一个聊天，绝不批量全量导出
- 数据、密钥、明文库禁止上传任何网络位置
- lldb `memory read -f x` 按 4 字节字打印小端值，解析用 `to_bytes(4,'little')`

## 参考

- 消息表/字段结构：`references/schema.md`
- 合成数据自测：`create_synthetic_ntqq_fixture.py` + `export_ntqq_chat.py`
