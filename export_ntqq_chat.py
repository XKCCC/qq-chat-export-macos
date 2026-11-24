#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shutil
import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CHAT_TYPE_TABLE = {
    "group": "group_msg_table",
    "c2c": "c2c_msg_table",
}

MSG_TYPE_NAMES = {
    0: "unknown",
    1: "blank",
    2: "text",
    3: "file",
    5: "image",
    6: "voice",
    7: "video",
    8: "system",
    9: "reply",
    10: "card",
    11: "emoji",
    16: "xml",
}


@dataclass
class ProtoField:
    number: int
    wire_type: int
    value: int | bytes


@dataclass
class ProtoMessage:
    path: tuple[int, ...]
    fields: list[ProtoField]


class ProtoDecodeError(ValueError):
    pass


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
    return row is not None


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def ts_to_text(value: int | None) -> str:
    if not value:
        return ""
    try:
        return dt.datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ProtoDecodeError("unexpected EOF")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            raise ProtoDecodeError("varint too long")


def parse_fields(data: bytes) -> list[ProtoField]:
    fields: list[ProtoField] = []
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number = key >> 3
        wire_type = key & 0x07
        if number <= 0:
            raise ProtoDecodeError("invalid field number")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ProtoDecodeError("unexpected EOF fixed64")
            value = int.from_bytes(data[offset:end], "little")
            offset = end
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ProtoDecodeError("unexpected EOF bytes")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ProtoDecodeError("unexpected EOF fixed32")
            value = int.from_bytes(data[offset:end], "little")
            offset = end
        else:
            raise ProtoDecodeError(f"unsupported wire type {wire_type}")
        fields.append(ProtoField(number, wire_type, value))
    return fields


def is_probably_text(data: bytes) -> bool:
    if not data or b"\x00" in data:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return printable / max(len(text), 1) > 0.9


def try_decode_text(data: bytes) -> str | None:
    if not data:
        return ""
    if not is_probably_text(data):
        return None
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


def walk_messages(data: bytes, max_depth: int = 8) -> list[ProtoMessage]:
    result: list[ProtoMessage] = []

    def _walk(blob: bytes, path: tuple[int, ...], depth: int) -> None:
        if depth > max_depth or not blob:
            return
        try:
            fields = parse_fields(blob)
        except ProtoDecodeError:
            return
        result.append(ProtoMessage(path, fields))
        for field in fields:
            if field.wire_type != 2 or not isinstance(field.value, bytes):
                continue
            if try_decode_text(field.value) is not None:
                continue
            _walk(field.value, path + (field.number,), depth + 1)

    _walk(data, tuple(), 0)
    return result


def field_first_int(fields: list[ProtoField], number: int) -> int | None:
    for field in fields:
        if field.number == number and isinstance(field.value, int):
            return int(field.value)
    return None


def field_first_string(fields: list[ProtoField], number: int) -> str | None:
    for field in fields:
        if field.number == number and isinstance(field.value, bytes):
            text = try_decode_text(field.value)
            if text:
                return text
    return None


def collect_strings(blob: bytes, interesting_fields: set[int]) -> list[str]:
    values: list[str] = []
    for message in walk_messages(blob):
        for field in message.fields:
            if field.number not in interesting_fields or not isinstance(field.value, bytes):
                continue
            text = try_decode_text(field.value)
            if text:
                values.append(text)
    return dedupe(values)


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = " ".join(str(value).split())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def shorten(text: str, limit: int = 500) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def human_size(value: int | None) -> str:
    if not value:
        return ""
    units = ["B", "KB", "MB", "GB"]
    size = float(value)
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{int(size)}{units[index]}" if index == 0 else f"{size:.1f}{units[index]}"


MEDIA_SUBDIRS = ("Pic", "Ptt", "Video")


def default_media_root() -> Path | None:
    base = Path.home() / "Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ"
    for path in sorted(base.glob("nt_qq_*/nt_data")):
        return path
    return None


def build_media_index(root: Path) -> dict[str, Path]:
    """Index media files by lowercase full name and stem; Ori (original) wins over Thumb."""
    index: dict[str, Path] = {}

    def score(path: Path) -> int:
        return 2 if "Ori" in path.parts else (1 if "Thumb" in path.parts else 0)

    for sub in MEDIA_SUBDIRS:
        directory = root / sub
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            for key in (path.name.lower(), path.stem.split("_")[0].lower()):
                old = index.get(key)
                if old is None or score(path) > score(old):
                    index[key] = path
    return index


def find_media_file(ref: str | None, root: Path, index: dict[str, Path]) -> Path | None:
    if not ref:
        return None
    candidate = Path(ref)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    relative = root / ref
    if relative.exists():
        return relative
    for key in (candidate.name.lower(), candidate.stem.split("_")[0].lower()):
        if key and key in index:
            return index[key]
    return None


def transcode_voice(src: Path) -> Path | None:
    """Transcode a QQ voice file to .m4a.

    QQ voice .amr files are actually SILK_V3 (magic ``#!SILK_V3``), which
    afconvert cannot read; decode them with pilk -> wav -> m4a. Real AMR files
    go straight through afconvert. Returns the m4a path, or None.
    """
    m4a = src.with_suffix(".m4a")
    header = src.read_bytes()[:12]
    if b"SILK_V3" in header:
        try:
            import pilk  # type: ignore
        except ImportError:
            return None
        import wave

        pcm = src.with_suffix(".pcm.tmp")
        wav = src.with_suffix(".wav.tmp")
        try:
            pilk.decode(str(src), str(pcm))
            with wave.open(str(wav), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(24000)
                handle.writeframes(pcm.read_bytes())
            result = subprocess.run(
                ["afconvert", "-f", "m4af", "-d", "aac", str(wav), str(m4a)],
                capture_output=True,
                timeout=60,
            )
        except Exception:
            return None
        finally:
            pcm.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)
        return m4a if result.returncode == 0 and m4a.exists() else None
    result = subprocess.run(
        ["afconvert", "-f", "m4af", "-d", "aac", str(src), str(m4a)],
        capture_output=True,
        timeout=60,
    )
    return m4a if result.returncode == 0 and m4a.exists() else None


def export_media(
    rows: list[dict[str, Any]],
    media_dir: Path,
    media_root: Path,
    out_dir: Path,
    transcode_audio: bool = False,
) -> dict[str, int]:
    """Copy referenced local media files into media_dir; annotate attachments with local_path."""
    index = build_media_index(media_root)
    stats = {"attachments": 0, "copied": 0, "missing": 0}
    used_names: set[str] = set()
    for row in rows:
        for attachment in row.get("attachments") or []:
            stats["attachments"] += 1
            src = None
            for ref in (attachment.get("file_path"), attachment.get("file_name"), attachment.get("thumb_path")):
                src = find_media_file(ref, media_root, index)
                if src is not None:
                    break
            if src is None:
                stats["missing"] += 1
                attachment["media_found"] = False
                continue
            timestamp = row.get("timestamp") or 0
            try:
                stamp = dt.datetime.fromtimestamp(int(timestamp)).strftime("%Y%m%d_%H%M%S")
            except Exception:
                stamp = "00000000_000000"
            base = f"{stamp}_{src.stem[:16]}{src.suffix.lower()}"
            name = base
            serial = 1
            while name in used_names:
                serial += 1
                name = f"{stamp}_{src.stem[:16]}_{serial}{src.suffix.lower()}"
            used_names.add(name)
            media_dir.mkdir(parents=True, exist_ok=True)
            dst = media_dir / name
            if not dst.exists():
                shutil.copy2(src, dst)
            if transcode_audio and dst.suffix.lower() == ".amr":
                converted = transcode_voice(dst)
                if converted is not None:
                    dst = converted
                    name = converted.name
            try:
                attachment["local_path"] = str(dst.relative_to(out_dir))
            except ValueError:
                attachment["local_path"] = str(dst)
            attachment["media_found"] = True
            stats["copied"] += 1
    return stats


def render_element(message: ProtoMessage) -> tuple[str | None, dict[str, Any] | None]:
    fields = message.fields
    element_type = field_first_int(fields, 45002)
    file_name = field_first_string(fields, 45402)
    file_path = field_first_string(fields, 45403)
    thumb_path = field_first_string(fields, 45954) or field_first_string(fields, 45422)
    size = field_first_int(fields, 45405)
    text = field_first_string(fields, 45101)
    transcript = field_first_string(fields, 45923) or field_first_string(fields, 45102)
    face_text = field_first_string(fields, 47602)
    reply_text = field_first_string(fields, 47413)
    reply_name = field_first_string(fields, 47421)
    xml_text = field_first_string(fields, 48602) or field_first_string(fields, 47901)

    attachment = None
    if file_name or file_path or thumb_path:
        attachment = {
            "element_type": element_type,
            "file_name": file_name,
            "file_path": file_path,
            "thumb_path": thumb_path,
            "size": size,
            "size_text": human_size(size),
        }

    if element_type == 1:
        return text, attachment
    if element_type == 2:
        return "[image]" + (f" {file_path}" if file_path else ""), attachment
    if element_type == 3:
        label = "[file]"
        if file_name:
            label += f" {file_name}"
        if size:
            label += f" ({human_size(size)})"
        return label, attachment
    if element_type == 4:
        return "[voice]" + (f" {transcript}" if transcript else ""), attachment
    if element_type == 5:
        return "[video]" + (f" {file_name or file_path}" if file_name or file_path else ""), attachment
    if element_type == 6:
        return face_text or "[emoji]", attachment
    if element_type == 7:
        if reply_text and reply_name:
            return f"[reply] {reply_name}: {reply_text}", attachment
        return f"[reply] {reply_text}" if reply_text else "[reply]", attachment
    if element_type == 8:
        return text or field_first_string(fields, 47713) or "[system]", attachment
    if element_type in (10, 16):
        return f"[card] {shorten(xml_text, 160)}" if xml_text else "[card]", attachment
    if element_type == 11:
        return face_text or "[market emoji]", attachment
    if text:
        return text, attachment
    if xml_text:
        return f"[message] {shorten(xml_text, 160)}", attachment
    return None, attachment


def decode_message_blob(blob: bytes | None) -> dict[str, Any]:
    if not blob:
        return {"text": "", "attachments": [], "element_types": []}
    parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    element_types: list[str] = []
    for message in walk_messages(blob):
        element_type = field_first_int(message.fields, 45002)
        if element_type is None:
            continue
        element_types.append(str(element_type))
        text, attachment = render_element(message)
        if text:
            parts.append(text)
        if attachment:
            attachments.append(attachment)
    if not parts:
        parts = collect_strings(blob, {45101, 45102, 45402, 45403, 45923, 47413, 47602, 47901, 48602})
    text = " ".join(dedupe(parts))
    if not text:
        text = f"[binary:{len(blob)}B]"
    return {"text": shorten(text), "attachments": attachments, "element_types": dedupe(element_types)}


def db_path(db_dir: Path, filename: str) -> Path:
    path = db_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"Missing required database: {path}")
    return path


def optional_db_path(db_dir: Path, filename: str) -> Path | None:
    path = db_dir / filename
    return path if path.exists() else None


def load_groups(db_dir: Path) -> dict[str, str]:
    path = optional_db_path(db_dir, "group_info.db")
    if not path:
        return {}
    groups: dict[str, str] = {}
    with connect(path) as conn:
        if not table_exists(conn, "group_list"):
            return groups
        detail_exists = table_exists(conn, "group_detail_info_ver1")
        if detail_exists:
            sql = """
            select g.[60001] as group_uin,
                   g.[60007] as group_name,
                   d.[60007] as detail_name,
                   d.[60026] as group_remark
            from group_list g
            left join group_detail_info_ver1 d on d.[60001] = g.[60001]
            """
        else:
            sql = "select [60001] as group_uin, [60007] as group_name, null as detail_name, null as group_remark from group_list"
        for row in conn.execute(sql):
            if row["group_uin"] is None:
                continue
            name = row["group_remark"] or row["detail_name"] or row["group_name"] or str(row["group_uin"])
            groups[str(row["group_uin"])] = name
    return groups


def load_group_members(db_dir: Path, group_uin: str | None = None) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    path = optional_db_path(db_dir, "group_info.db")
    by_uid: dict[tuple[str, str], str] = {}
    by_uin: dict[tuple[str, str], str] = {}
    if not path:
        return by_uid, by_uin
    with connect(path) as conn:
        if not table_exists(conn, "group_member3"):
            return by_uid, by_uin
        params: tuple[Any, ...] = ()
        where = ""
        if group_uin:
            where = "where [60001] = ?"
            params = (int(group_uin),) if str(group_uin).isdigit() else (group_uin,)
        rows = conn.execute(
            f"""
            select [60001] as group_uin,
                   [1000] as nt_uid,
                   [1002] as uin,
                   [20002] as nickname,
                   [64003] as group_nick
            from group_member3
            {where}
            """,
            params,
        ).fetchall()
    for row in rows:
        group = str(row["group_uin"] or "")
        name = row["group_nick"] or row["nickname"] or str(row["uin"] or "") or row["nt_uid"] or ""
        if group and row["nt_uid"]:
            by_uid[(group, str(row["nt_uid"]))] = name
        if group and row["uin"] is not None:
            by_uin[(group, str(row["uin"]))] = name
    return by_uid, by_uin


def load_profiles(db_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    path = optional_db_path(db_dir, "profile_info.db")
    by_uid: dict[str, dict[str, Any]] = {}
    by_uin: dict[str, dict[str, Any]] = {}
    if not path:
        return by_uid, by_uin
    with connect(path) as conn:
        if not table_exists(conn, "profile_info_v6"):
            return by_uid, by_uin
        rows = conn.execute(
            """
            select [1000] as nt_uid,
                   [1001] as qid,
                   [1002] as uin,
                   [20002] as nickname,
                   [20009] as remark
            from profile_info_v6
            """
        ).fetchall()
    for row in rows:
        display = row["remark"] or row["nickname"] or row["qid"] or str(row["uin"] or "") or row["nt_uid"] or ""
        item = dict(row)
        item["display_name"] = display
        if row["nt_uid"]:
            by_uid[str(row["nt_uid"])] = item
        if row["uin"] is not None:
            by_uin[str(row["uin"])] = item
    return by_uid, by_uin


def list_groups(db_dir: Path, query: str = "", limit: int = 200) -> None:
    groups = load_groups(db_dir)
    needle = query.lower()
    count = 0
    for group_uin, name in sorted(groups.items(), key=lambda item: item[1]):
        if needle and needle not in name.lower() and needle not in group_uin:
            continue
        print(f"{name}\t{group_uin}")
        count += 1
        if count >= limit:
            break


def list_c2c(db_dir: Path, query: str = "", limit: int = 200) -> None:
    by_uid, by_uin = load_profiles(db_dir)
    seen: set[str] = set()
    needle = query.lower()
    count = 0
    for item in sorted(by_uid.values(), key=lambda value: value["display_name"]):
        key = str(item.get("nt_uid") or item.get("uin") or "")
        if key in seen:
            continue
        seen.add(key)
        haystack = " ".join(str(item.get(k) or "") for k in ("display_name", "nt_uid", "uin", "qid"))
        if needle and needle not in haystack.lower():
            continue
        print(f"{item['display_name']}\tuin={item.get('uin') or ''}\tnt_uid={item.get('nt_uid') or ''}")
        count += 1
        if count >= limit:
            break


def pre_ids(pre_msg_db: Path | None, chat_type: str, peer: str) -> set[int]:
    if not pre_msg_db:
        return set()
    table = CHAT_TYPE_TABLE[chat_type]
    with connect(pre_msg_db) as conn:
        if not table_exists(conn, table):
            return set()
        where, params = chat_filter(chat_type, peer)
        return {int(row[0]) for row in conn.execute(f"select [40001] from {table} where {where}", params) if row[0] is not None}


def chat_filter(chat_type: str, peer: str) -> tuple[str, tuple[Any, ...]]:
    if chat_type == "group":
        values: list[Any] = [peer]
        clauses = ["[40021] = ?"]
        if peer.isdigit():
            clauses.append("[40030] = ?")
            values.append(int(peer))
        return "(" + " or ".join(clauses) + ")", tuple(values)
    values = [peer]
    clauses = ["[40021] = ?", "[40020] = ?"]
    if peer.isdigit():
        clauses.append("[40030] = ?")
        values.append(int(peer))
    values.insert(1, peer)
    return "(" + " or ".join(clauses) + ")", tuple(values)


def resolve_sender(
    chat_type: str,
    row: sqlite3.Row,
    member_by_uid: dict[tuple[str, str], str],
    member_by_uin: dict[tuple[str, str], str],
    profile_by_uid: dict[str, dict[str, Any]],
    profile_by_uin: dict[str, dict[str, Any]],
) -> str:
    sender_uid = str(row["sender_uid"] or "")
    sender_uin = str(row["sender_uin"] or "")
    peer_key = str(row["peer_key"] or "")
    if chat_type == "group":
        return (
            row["sender_member_name"]
            or row["sender_nickname"]
            or member_by_uid.get((peer_key, sender_uid))
            or member_by_uin.get((peer_key, sender_uin))
            or sender_uin
            or sender_uid
            or ""
        )
    profile = profile_by_uid.get(sender_uid) or profile_by_uin.get(sender_uin)
    if profile:
        return profile["display_name"]
    return row["sender_nickname"] or sender_uin or sender_uid or ""


def load_rows(args: argparse.Namespace) -> tuple[list[dict[str, Any]], str]:
    db_dir = Path(args.db_dir).expanduser().resolve()
    msg_db = db_path(db_dir, "nt_msg.db")
    table = CHAT_TYPE_TABLE[args.chat_type]
    groups = load_groups(db_dir)
    member_by_uid, member_by_uin = load_group_members(db_dir, args.peer if args.chat_type == "group" else None)
    profile_by_uid, profile_by_uin = load_profiles(db_dir)
    previous_ids = pre_ids(Path(args.pre_msg_db).expanduser().resolve() if args.pre_msg_db else None, args.chat_type, args.peer)

    where, params = chat_filter(args.chat_type, args.peer)
    with connect(msg_db) as conn:
        if not table_exists(conn, table):
            raise RuntimeError(f"Missing table {table} in {msg_db}")
        rows = conn.execute(
            f"""
            select [40001] as msg_id,
                   [40002] as msg_random,
                   [40003] as msg_seq,
                   [40010] as chat_type,
                   [40011] as msg_type,
                   [40012] as sub_msg_type,
                   [40013] as send_type,
                   [40020] as sender_uid,
                   [40021] as peer_key,
                   [40030] as peer_uin,
                   [40033] as sender_uin,
                   [40050] as msg_time,
                   [40090] as sender_member_name,
                   [40093] as sender_nickname,
                   [40800] as body
            from {table}
            where {where}
            order by coalesce([40050], 0), coalesce([40003], 0), [40001]
            """,
            params,
        ).fetchall()

    if args.chat_type == "group":
        chat_name = groups.get(str(args.peer), str(args.peer))
    else:
        profile = profile_by_uid.get(args.peer) or profile_by_uin.get(args.peer)
        chat_name = profile["display_name"] if profile else str(args.peer)

    output: list[dict[str, Any]] = []
    self_uin = int(args.self_uin) if args.self_uin and str(args.self_uin).isdigit() else None
    for row in rows:
        decoded = decode_message_blob(row["body"])
        sender = resolve_sender(args.chat_type, row, member_by_uid, member_by_uin, profile_by_uid, profile_by_uin)
        sender_uin = row["sender_uin"]
        output.append(
            {
                "chat_name": chat_name,
                "chat_type": args.chat_type,
                "peer": args.peer,
                "msg_id": row["msg_id"],
                "msg_random": row["msg_random"],
                "msg_seq": row["msg_seq"],
                "msg_type": row["msg_type"],
                "msg_type_name": MSG_TYPE_NAMES.get(row["msg_type"], f"unknown:{row['msg_type']}"),
                "sub_msg_type": row["sub_msg_type"],
                "send_type": row["send_type"],
                "sender": sender,
                "sender_uid": row["sender_uid"],
                "sender_uin": sender_uin,
                "is_self": bool(self_uin is not None and sender_uin == self_uin),
                "timestamp": row["msg_time"],
                "time": ts_to_text(row["msg_time"]),
                "text": decoded["text"],
                "attachments": decoded["attachments"],
                "element_types": decoded["element_types"],
                "is_new_after_import_snapshot": row["msg_id"] not in previous_ids if previous_ids else False,
            }
        )
    return output, chat_name


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "time",
        "timestamp",
        "chat_name",
        "chat_type",
        "peer",
        "sender",
        "sender_uin",
        "is_self",
        "msg_seq",
        "msg_id",
        "msg_type_name",
        "text",
        "attachments",
        "is_new_after_import_snapshot",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            item = {key: row.get(key, "") for key in fields}
            item["attachments"] = json.dumps(row.get("attachments", []), ensure_ascii=False)
            writer.writerow(item)


def write_text(path: Path, rows: list[dict[str, Any]], title: str) -> None:
    lines = [title, f"message_count={len(rows)}", ""]
    for row in rows:
        text = row.get("text") or "[no decodable text]"
        media_refs = [
            attachment["local_path"]
            for attachment in row.get("attachments") or []
            if attachment.get("local_path")
        ]
        if media_refs:
            text += " " + " ".join(f"-> {ref}" for ref in media_refs)
        self_mark = " self" if row.get("is_self") else ""
        new_mark = " new_after_import_snapshot" if row.get("is_new_after_import_snapshot") else ""
        attachments = ""
        if row.get("attachments"):
            attachments = "\nattachments=" + json.dumps(row["attachments"], ensure_ascii=False)
        lines.append(
            f"[{row.get('time') or '-'}] {row.get('sender') or ''}({row.get('sender_uin') or ''})"
            f"{self_mark}{new_mark} seq={row.get('msg_seq')} id={row.get('msg_id')} type={row.get('msg_type_name')}\n"
            f"{text}{attachments}\n"
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_summary(path: Path, rows: list[dict[str, Any]], args: argparse.Namespace, chat_name: str) -> None:
    dated = [row for row in rows if row.get("time")]
    self_rows = [row for row in rows if row.get("is_self")]
    new_rows = [row for row in rows if row.get("is_new_after_import_snapshot")]
    lines = [
        "NTQQ chat export summary",
        f"chat_name={chat_name}",
        f"chat_type={args.chat_type}",
        f"peer={args.peer}",
        f"message_count={len(rows)}",
        f"messages_with_time={len(dated)}",
        f"self_messages={len(self_rows)}",
        f"new_after_import_snapshot={len(new_rows)}",
        f"first_time={min((row['time'] for row in dated if row['time']), default='')}",
        f"last_time={max((row['time'] for row in dated if row['time']), default='')}",
        "",
        "date,message_count,self_count,new_after_import_snapshot_count",
    ]
    daily = Counter(row["time"][:10] for row in dated)
    self_daily = Counter(row["time"][:10] for row in dated if row.get("is_self"))
    new_daily = Counter(row["time"][:10] for row in dated if row.get("is_new_after_import_snapshot"))
    for day in sorted(daily):
        lines.append(f"{day},{daily[day]},{self_daily[day]},{new_daily[day]}")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_daily_counts(path: Path, rows: list[dict[str, Any]]) -> None:
    dated = [row for row in rows if row.get("time")]
    daily = Counter(row["time"][:10] for row in dated)
    self_daily = Counter(row["time"][:10] for row in dated if row.get("is_self"))
    new_daily = Counter(row["time"][:10] for row in dated if row.get("is_new_after_import_snapshot"))
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "message_count", "self_count", "new_after_import_snapshot_count"])
        writer.writeheader()
        for day in sorted(daily):
            writer.writerow(
                {
                    "date": day,
                    "message_count": daily[day],
                    "self_count": self_daily[day],
                    "new_after_import_snapshot_count": new_daily[day],
                }
            )


def write_keyword_hits(prefix: Path, rows: list[dict[str, Any]], keywords: list[str]) -> None:
    if not keywords:
        return
    hits: list[dict[str, Any]] = []
    for row in rows:
        haystack = " ".join(
            [
                str(row.get("time") or ""),
                str(row.get("sender") or ""),
                str(row.get("sender_uin") or ""),
                str(row.get("text") or ""),
                json.dumps(row.get("attachments") or [], ensure_ascii=False),
            ]
        )
        matched = [keyword for keyword in keywords if keyword and keyword in haystack]
        if not matched:
            continue
        item = dict(row)
        item["matched_keywords"] = matched
        hits.append(item)
    write_json(prefix.with_name(prefix.name + "_keyword_hits.json"), hits)
    write_csv(prefix.with_name(prefix.name + "_keyword_hits.csv"), hits)
    lines = [f"keyword_hits={len(hits)}", f"keywords={','.join(keywords)}", ""]
    for row in hits:
        lines.append(
            f"[{row.get('time') or '-'}] {row.get('sender') or ''}({row.get('sender_uin') or ''}) "
            f"kw={','.join(row['matched_keywords'])} seq={row.get('msg_seq')} id={row.get('msg_id')}\n"
            f"{row.get('text') or '[no decodable text]'}\n"
        )
    prefix.with_name(prefix.name + "_keyword_hits.txt").write_text("\n".join(lines), encoding="utf-8")


def write_date_extract(prefix: Path, rows: list[dict[str, Any]], date_value: str | None) -> None:
    if not date_value:
        return
    filtered = [row for row in rows if str(row.get("time") or "").startswith(date_value)]
    suffix = "_" + date_value
    write_json(prefix.with_name(prefix.name + suffix + ".json"), filtered)
    write_csv(prefix.with_name(prefix.name + suffix + ".csv"), filtered)
    write_text(prefix.with_name(prefix.name + suffix + ".txt"), filtered, f"Messages on {date_value}")


def write_new_extract(prefix: Path, rows: list[dict[str, Any]]) -> None:
    filtered = [row for row in rows if row.get("is_new_after_import_snapshot")]
    if not filtered:
        return
    write_json(prefix.with_name(prefix.name + "_new_after_import.json"), filtered)
    write_csv(prefix.with_name(prefix.name + "_new_after_import.csv"), filtered)
    write_text(prefix.with_name(prefix.name + "_new_after_import.txt"), filtered, "New messages after import snapshot")


def parse_keywords(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export decrypted NTQQ chat history to TXT, JSON, and CSV.")
    parser.add_argument("--db-dir", required=True, help="Directory containing decrypted nt_msg.db and optional group_info.db/profile_info.db.")
    parser.add_argument("--list-groups", action="store_true", help="List groups from group_info.db and exit.")
    parser.add_argument("--list-c2c", action="store_true", help="List one-to-one profile targets from profile_info.db and exit.")
    parser.add_argument("--query", default="", help="Filter for --list-groups or --list-c2c.")
    parser.add_argument("--limit", type=int, default=200, help="List limit for --list-groups or --list-c2c.")
    parser.add_argument("--chat-type", choices=sorted(CHAT_TYPE_TABLE), help="Target chat type.")
    parser.add_argument("--peer", help="Group UIN for group chats, or NT UID/UIN for c2c chats.")
    parser.add_argument("--self-uin", default="", help="Optional current account UIN; marks is_self.")
    parser.add_argument("--out-dir", default="outputs", help="Output directory.")
    parser.add_argument("--prefix", default="ntqq_export", help="Output filename prefix.")
    parser.add_argument("--keywords", default="", help="Comma-separated keyword list for focused hit files.")
    parser.add_argument("--date", default="", help="Optional YYYY-MM-DD date extract.")
    parser.add_argument("--pre-msg-db", default="", help="Optional pre-import decrypted nt_msg.db for message-id diff.")
    parser.add_argument("--with-media", action="store_true", help="Copy locally-cached image/voice/video files into <prefix>_media/ and annotate attachments with local_path.")
    parser.add_argument("--media-root", default="", help="Path to the NTQQ nt_data directory (auto-detected from the QQ container when omitted).")
    parser.add_argument("--transcode-audio", action="store_true", help="Also convert copied .amr voice files to .m4a via afconvert.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    db_dir = Path(args.db_dir).expanduser().resolve()

    if args.list_groups:
        list_groups(db_dir, args.query, args.limit)
        return 0
    if args.list_c2c:
        list_c2c(db_dir, args.query, args.limit)
        return 0
    if not args.chat_type or not args.peer:
        parser.error("--chat-type and --peer are required unless listing targets")

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / args.prefix

    rows, chat_name = load_rows(args)
    if args.with_media:
        media_root = Path(args.media_root).expanduser().resolve() if args.media_root else default_media_root()
        if media_root is None or not media_root.exists():
            raise RuntimeError("--with-media needs a valid nt_data dir (use --media-root)")
        stats = export_media(rows, out_dir / f"{args.prefix}_media", media_root, out_dir, args.transcode_audio)
        print(f"media_attachments={stats['attachments']} media_copied={stats['copied']} media_missing={stats['missing']}")
    write_json(prefix.with_name(prefix.name + "_full.json"), rows)
    write_csv(prefix.with_name(prefix.name + "_full.csv"), rows)
    write_text(prefix.with_name(prefix.name + "_full.txt"), rows, f"NTQQ export: {chat_name}")
    write_summary(prefix.with_name(prefix.name + "_summary.txt"), rows, args, chat_name)
    write_daily_counts(prefix.with_name(prefix.name + "_daily_counts.csv"), rows)
    write_keyword_hits(prefix, rows, parse_keywords(args.keywords))
    write_date_extract(prefix, rows, args.date or None)
    write_new_extract(prefix, rows)

    print(f"chat_name={chat_name}")
    print(f"message_count={len(rows)}")
    print(f"out_dir={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
