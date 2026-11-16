# NTQQ SQLite Schema Notes

These notes cover the NTQQ schema fields used by `scripts/export_ntqq_chat.py`. Numeric column names can change across QQ versions. Prefer defensive SQL and `PRAGMA table_info(...)` checks when extending the exporter.

## Required Files

| File | Required | Purpose |
|---|---:|---|
| `nt_msg.db` | Yes | Message rows and recent sessions |
| `group_info.db` | Optional | Group names and group member names |
| `profile_info.db` | Optional | Friend names and UIN mappings |

## Message Tables

| Table | Chat type |
|---|---|
| `group_msg_table` | Group chats |
| `c2c_msg_table` | One-to-one chats |

## Message Columns

| Column | Meaning used by exporter |
|---|---|
| `40001` | Message ID |
| `40002` | Message random |
| `40003` | Message sequence |
| `40010` | Chat type |
| `40011` | Message type |
| `40012` | Sub message type |
| `40013` | Send type |
| `40020` | Sender NT UID in group rows; peer/sender UID in c2c rows |
| `40021` | Group UIN for group rows; peer NT UID for c2c rows |
| `40030` | Peer UIN |
| `40033` | Sender UIN |
| `40050` | Unix timestamp |
| `40090` | Sender member name |
| `40093` | Sender nickname |
| `40800` | Protobuf message body |

## Group Info Tables

| Table | Columns |
|---|---|
| `group_list` | `60001` group UIN, `60007` group name |
| `group_detail_info_ver1` | `60001` group UIN, `60007` detail name, `60026` group remark |
| `group_member3` | `60001` group UIN, `1000` NT UID, `1002` UIN, `20002` nickname, `64003` group nickname |

## Profile Tables

| Table | Columns |
|---|---|
| `profile_info_v6` | `1000` NT UID, `1001` QID, `1002` UIN, `20002` nickname, `20009` remark |

## Message Body Decoding

`40800` is a protobuf-like blob. The exporter includes a small wire parser and extracts common text and attachment fields:

| Field | Meaning |
|---|---|
| `45002` | Element type |
| `45101` | Text |
| `45102` | Alternate text / voice transcript |
| `45402` | File name |
| `45403` | File path |
| `45405` | File size |
| `45923` | Voice transcript |
| `47413` | Reply text |
| `47421` | Reply sender name |
| `47602` | Face / emoji text |
| `47901`, `48602` | Card/XML text |

The decoded text is best-effort. If QQ changes element fields, update the field map in the script.
