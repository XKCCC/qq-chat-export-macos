#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return encode_varint((number << 3) | 0) + encode_varint(value)


def field_bytes(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def text_body(text: str) -> bytes:
    # Minimal nested element with element_type=1 and text field 45101.
    inner = field_varint(45002, 1) + field_bytes(45101, text.encode("utf-8"))
    return field_bytes(1, inner)


def create_nt_msg(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table group_msg_table(
          [40001] integer primary key,
          [40002] integer,
          [40003] integer,
          [40010] integer,
          [40011] integer,
          [40012] integer,
          [40013] integer,
          [40020] text,
          [40021] text,
          [40030] integer,
          [40033] integer,
          [40050] integer,
          [40090] text,
          [40093] text,
          [40800] blob
        );
        create table c2c_msg_table(
          [40001] integer primary key,
          [40002] integer,
          [40003] integer,
          [40010] integer,
          [40011] integer,
          [40012] integer,
          [40013] integer,
          [40020] text,
          [40021] text,
          [40030] integer,
          [40033] integer,
          [40050] integer,
          [40090] text,
          [40093] text,
          [40800] blob
        );
        """
    )
    con.executemany(
        """
        insert into group_msg_table
        ([40001],[40002],[40003],[40010],[40011],[40012],[40013],[40020],[40021],[40030],[40033],[40050],[40090],[40093],[40800])
        values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (1001, 11, 1, 2, 2, 1, 0, "u_sender_a", "123456", 123456, 111001, 1700000000, "Alice", "", text_body("Hello from synthetic group.")),
            (1002, 12, 2, 2, 2, 1, 0, "u_self", "123456", 123456, 100000, 1700000060, "Self User", "", text_body("Synthetic onboarding keyword.")),
            (1003, 13, 3, 2, 2, 1, 0, "u_sender_b", "123456", 123456, 111002, 1700086400, "Bob", "", text_body("Another synthetic message.")),
        ],
    )
    con.executemany(
        """
        insert into c2c_msg_table
        ([40001],[40002],[40003],[40010],[40011],[40012],[40013],[40020],[40021],[40030],[40033],[40050],[40090],[40093],[40800])
        values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (2001, 21, 1, 1, 2, 1, 0, "u_friend", "u_friend", 222001, 222001, 1700001000, "", "Friend", text_body("Hello from synthetic c2c.")),
        ],
    )
    con.commit()
    con.close()


def create_group_info(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table group_list([60001] integer primary key, [60007] text);
        create table group_detail_info_ver1([60001] integer primary key, [60007] text, [60026] text);
        create table group_member3([60001] integer, [1000] text, [1002] integer, [20002] text, [64003] text);
        """
    )
    con.execute("insert into group_list values (?, ?)", (123456, "Synthetic Group"))
    con.execute("insert into group_detail_info_ver1 values (?, ?, ?)", (123456, "Synthetic Group", ""))
    con.executemany(
        "insert into group_member3 values (?, ?, ?, ?, ?)",
        [
            (123456, "u_sender_a", 111001, "Alice", "Alice"),
            (123456, "u_self", 100000, "Self User", "Self User"),
            (123456, "u_sender_b", 111002, "Bob", "Bob"),
        ],
    )
    con.commit()
    con.close()


def create_profile_info(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("create table profile_info_v6([1000] text primary key, [1001] text, [1002] integer, [20002] text, [20009] text)")
    con.executemany(
        "insert into profile_info_v6 values (?, ?, ?, ?, ?)",
        [
            ("u_friend", "friend_qid", 222001, "Friend", ""),
            ("u_self", "self_qid", 100000, "Self User", ""),
        ],
    )
    con.commit()
    con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a synthetic decrypted NTQQ fixture with no personal data.")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    out_dir = Path(args.out_dir).expanduser().resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    create_nt_msg(out_dir / "nt_msg.db")
    create_group_info(out_dir / "group_info.db")
    create_profile_info(out_dir / "profile_info.db")
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
