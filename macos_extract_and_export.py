#!/usr/bin/env python3
"""
macOS (Apple Silicon) one-shot NTQQ chat export: extract the local database key
from a re-signed QQ copy, decrypt nt_msg/group_info/profile_info, and export
one chat via export_ntqq_chat.py.

How it works:
  1. APFS-clone /Applications/QQ.app into a temp dir and ad-hoc re-sign it with
     get-task-allow (the original app is never modified, no sudo needed).
  2. Stage the real data at ~/Library/Application Support/QQ/ because an
     unsandboxed QQ resolves its data dir outside the container:
     copy `auth/`, clone `nt_qq_*/nt_db`, symlink `nt_data`.
  3. Launch the re-signed copy under lldb with a breakpoint on
     PKCS5_PBKDF2_HMAC (QQ's bundled libcrypto.dylib) and capture the
     passphrase + salt for the target DBs (matched by each file's salt).
  4. Strip the 1024-byte "QQ_NT DB" header and decrypt with sqlcipher
     (page 4096, kdf_iter 4000, HMAC_SHA1, PBKDF2_HMAC_SHA512).
  5. Run export_ntqq_chat.py for the requested chat only.
  6. Clean up: kill the copy (matched by its /tmp path, never the original),
     restore the staging directory, delete the temp dir (key and plaintext
     included).

Key-extraction technique credits (public research): QQBackup/nt_msg_db_util,
Mythologyli/qq-nt-db, dxfxbfg/qq-cli, jielosc/qqnt-export-macos.
The staging/restore and salt-matching automation here is original.

Requires: brew install sqlcipher; Xcode CLT (lldb). macOS arm64.
Use only for chat history of your own account on your own machine.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
CONTAINER_QQ = HOME / "Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ"
QQDATA = HOME / "Library/Application Support/QQ"
EXPORTER = Path(__file__).resolve().parent / "export_ntqq_chat.py"
TARGET_DBS = ("nt_msg", "group_info", "profile_info")

ENTITLEMENTS = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>com.apple.security.cs.get-task-allow</key>
	<true/>
	<key>com.apple.security.cs.allow-jit</key>
	<true/>
	<key>com.apple.security.cs.disable-library-validation</key>
	<true/>
	<key>com.apple.security.network.client</key>
	<true/>
	<key>com.apple.security.network.server</key>
	<true/>
</dict>
</plist>
"""

LLDB_CMDS = """breakpoint set --name PKCS5_PBKDF2_HMAC --shlib libcrypto.dylib
breakpoint command add 1
p (unsigned int)$w1
p (unsigned int)$w3
p (unsigned int)$w4
p (unsigned int)$w6
memory read --force -f x -c 64 $x0
memory read --force -f x -c 16 $x2
continue
DONE
run
"""


def log(msg: str) -> None:
    print(f"[qq-export] {msg}", flush=True)


def fail(msg: str, code: int = 1) -> None:
    print(f"[qq-export][ERROR] {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def preflight() -> None:
    if run(["pgrep", "-x", "QQ"]).returncode == 0:
        fail("Original QQ is running. Quit it (Cmd+Q) and re-run this script.")
    if shutil.which("sqlcipher") is None:
        fail("sqlcipher not found: brew install sqlcipher")
    if shutil.which("lldb") is None:
        fail("lldb not found: xcode-select --install")
    if not CONTAINER_QQ.exists():
        fail(f"QQ container data dir not found: {CONTAINER_QQ}")
    if not EXPORTER.exists():
        fail(f"export_ntqq_chat.py not found next to this script: {EXPORTER}")


def find_nt_hash() -> str:
    dirs = [p.name for p in CONTAINER_QQ.glob("nt_qq_*") if (p / "nt_db" / "nt_msg.db").exists()]
    if not dirs:
        fail("No nt_qq_*/nt_db/nt_msg.db found in the QQ container")
    return sorted(dirs)[0]


def resign_copy(work: Path) -> Path:
    app = work / "QQ.app"
    log("Cloning QQ.app (APFS clone; original untouched)...")
    r = run(["cp", "-Rc", "/Applications/QQ.app", str(app)])
    if r.returncode != 0:
        fail(f"failed to clone QQ.app: {r.stderr.strip()}")
    ent = work / "entitlements.plist"
    ent.write_text(ENTITLEMENTS)
    log("Ad-hoc re-signing the copy with get-task-allow...")
    r = run(["codesign", "--force", "--deep", "--sign", "-", "--entitlements", str(ent), str(app)], timeout=300)
    if r.returncode != 0:
        fail(f"codesign failed: {r.stderr}")
    if "adhoc" not in run(["codesign", "-dv", str(app)]).stderr:
        fail("re-sign verification failed")
    return app


class Staging:
    """Stage real data at ~/Library/Application Support/QQ/ and restore exactly.

    Snapshot-based: anything that did not exist before build() is removed by
    cleanup(); a pre-existing auth/ is moved aside and moved back.
    """

    def __init__(self, nt_hash: str):
        self.nt_hash = nt_hash
        self.qqdata_existed = QQDATA.exists()
        self.before = set(os.listdir(QQDATA)) if self.qqdata_existed else set()
        self.moved_aside: list[tuple[Path, Path]] = []

    def build(self) -> None:
        QQDATA.mkdir(exist_ok=True)
        src_auth = CONTAINER_QQ / "auth"
        dst_auth = QQDATA / "auth"
        if src_auth.exists():
            if dst_auth.exists():
                bak = QQDATA / "auth.qqexp_bak"
                shutil.move(str(dst_auth), str(bak))
                self.moved_aside.append((bak, dst_auth))
            shutil.copytree(src_auth, dst_auth)
        target = QQDATA / self.nt_hash
        target.mkdir()
        src = CONTAINER_QQ / self.nt_hash
        r = run(["cp", "-Rc", str(src / "nt_db"), str(target / "nt_db")], timeout=300)
        if r.returncode != 0:
            self.cleanup()
            fail(f"failed to clone nt_db: {r.stderr.strip()}")
        os.symlink(src / "nt_data", target / "nt_data")
        (target / "nt_temp").mkdir()
        log(f"Staging ready: {target} (nt_db cloned, nt_data symlinked, auth copied)")

    def cleanup(self) -> None:
        if not QQDATA.exists():
            return
        backups = {bak.name for bak, _ in self.moved_aside}
        for p in QQDATA.iterdir():
            if p.name not in self.before and p.name not in backups:
                if p.is_symlink() or p.is_file():
                    p.unlink(missing_ok=True)
                else:
                    shutil.rmtree(p, ignore_errors=True)
        for bak, orig in self.moved_aside:
            if orig.exists():
                shutil.rmtree(orig, ignore_errors=True)
            if bak.exists():
                shutil.move(str(bak), str(orig))
        if not self.qqdata_existed:
            shutil.rmtree(QQDATA, ignore_errors=True)
        log("Staging directory restored")


def parse_hits(logpath: Path) -> list[dict]:
    # lldb `memory read -f x` prints 4-byte words as little-endian values;
    # convert each word back with to_bytes(4, 'little').
    try:
        text = logpath.read_text(errors="replace")
    except FileNotFoundError:
        return []
    lines = text.splitlines()
    hits, cur, mem_target = [], None, None
    for i, line in enumerate(lines):
        m = re.match(r"\(lldb\)\s+p \(unsigned int\)\$(w[0-9]+)", line)
        if m:
            cur = cur or {}
            if i + 1 < len(lines):
                vm = re.match(r"\(unsigned int\) (\d+)", lines[i + 1])
                if vm:
                    cur[m.group(1)] = int(vm.group(1))
            continue
        m = re.match(r"\(lldb\)\s+memory read .* \$x([02])", line)
        if m:
            cur = cur or {}
            mem_target = "pass" if m.group(1) == "0" else "salt"
            cur.setdefault(mem_target, b"")
            continue
        m = re.match(r"^0x[0-9a-f]+: ((?:0x[0-9a-f]+\s*)+)$", line)
        if m and cur is not None and mem_target:
            for word in m.group(1).split():
                cur[mem_target] += int(word, 16).to_bytes(4, "little")
            continue
        if re.match(r"\(lldb\)\s+continue", line):
            if cur and "w1" in cur and cur.get("salt"):
                hits.append(cur)
            cur, mem_target = None, None
    return hits


def db_salts(nt_db_dir: Path) -> dict[bytes, str]:
    out = {}
    for name in TARGET_DBS:
        data = (nt_db_dir / f"{name}.db").read_bytes()
        off = 1024 if data[:16] == b"SQLite header 3\x00" else 0
        out[data[off:off + 16]] = name
    return out


def extract_key(app: Path, work: Path, salts: dict[bytes, str], timeout: int) -> bytes:
    cmds = work / "lldb_cmds.txt"
    cmds.write_text(LLDB_CMDS)
    logfile = work / "lldb.log"
    binpath = app / "Contents/MacOS/QQ"
    log("Launching the re-signed copy under lldb; waiting for the db key "
        "(scan the QR code if a login window appears)...")
    with open(logfile, "w") as lf:
        proc = subprocess.Popen(
            ["lldb", "-s", str(cmds), str(binpath)],
            stdin=subprocess.PIPE, stdout=lf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    key = None
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            for h in parse_hits(logpath):
                salt = h.get("salt", b"")[: h.get("w3", 0)]
                if salt in salts and h.get("w4") == 4000:
                    p = h.get("pass", b"")[: h.get("w1", 0)]
                    if p:
                        log(f"key captured (db: {salts[salt]}, {len(p)} chars)")
                        key = p
                        break
            else:
                time.sleep(2)
                continue
            break
        if key is None:
            fail("timed out waiting for the key. Make sure the copy logs in "
                 "and the chat list loads, then re-run.")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(2)
    r = run(["pgrep", "-f", f"{work}/QQ.app/Contents/MacOS/QQ"])
    for pid in r.stdout.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(2)
    return key


def sqlcipher_ok(db: Path, key: bytes) -> bool:
    sql = (
        "PRAGMA cipher_page_size = 4096;\n"
        f"PRAGMA key = '{key.decode(errors='replace').replace(chr(39), chr(39) * 2)}';\n"
        "PRAGMA kdf_iter = 4000;\n"
        "PRAGMA cipher_hmac_algorithm = HMAC_SHA1;\n"
        "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;\n"
        "SELECT count(*) FROM sqlite_master;\n"
    )
    r = run(["sqlcipher", str(db)], input=sql, timeout=120)
    return any(l.strip().isdigit() and int(l.strip()) > 0 for l in (r.stdout + r.stderr).splitlines())


def decrypt_all(work: Path, staged_nt_db: Path, key: bytes) -> Path:
    clean = work / "clean"
    plain = work / "plain"
    clean.mkdir()
    plain.mkdir()
    for name in TARGET_DBS:
        data = (staged_nt_db / f"{name}.db").read_bytes()
        off = 1024 if data[:16] == b"SQLite header 3\x00" else 0
        (clean / f"{name}.db").write_bytes(data[off:])
        wal = staged_nt_db / f"{name}.db-wal"
        if wal.exists() and wal.stat().st_size > 0:
            shutil.copy2(wal, clean / f"{name}.db-wal")
    if not sqlcipher_ok(clean / "group_info.db", key):
        fail("key validation failed on group_info.db; this QQ version may use "
             "different SQLCipher parameters")
    key_sql = key.decode(errors="replace").replace("'", "''")
    for name in TARGET_DBS:
        sql = (
            "PRAGMA cipher_page_size = 4096;\n"
            f"PRAGMA key = '{key_sql}';\n"
            "PRAGMA kdf_iter = 4000;\n"
            "PRAGMA cipher_hmac_algorithm = HMAC_SHA1;\n"
            "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;\n"
            f"ATTACH DATABASE '{plain}/{name}.db' AS plaintext KEY '';\n"
            "SELECT sqlcipher_export('plaintext');\n"
            "DETACH DATABASE plaintext;\n"
        )
        r = run(["sqlcipher", str(clean / f"{name}.db")], input=sql, timeout=600)
        if not (plain / f"{name}.db").exists():
            fail(f"decrypting {name}.db failed: {r.stdout}{r.stderr}")
        log(f"decrypted: {name}.db")
    return plain


def resolve_chat(plain: Path, peer: str, chat_type: str | None) -> tuple[str, str, str]:
    import sqlite3
    if chat_type == "group" or (chat_type is None and peer.isdigit()):
        conn = sqlite3.connect(plain / "group_info.db")
        row = conn.execute("select [60001],[60007] from group_list where [60001]=?", (peer,)).fetchone()
        conn.close()
        if row:
            return "group", str(row[0]), row[1] or peer
        if chat_type == "group":
            fail(f"group {peer} not found in group_list")
    if chat_type in (None, "c2c"):
        conn = sqlite3.connect(plain / "profile_info.db")
        if peer.isdigit():
            row = conn.execute("select [1000],[20002],[20009] from profile_info_v6 where [1002]=?", (peer,)).fetchone()
        else:
            row = conn.execute("select [1000],[20002],[20009] from profile_info_v6 where [1000]=?", (peer,)).fetchone()
        conn.close()
        if row:
            name = row[2] or row[1] or peer
            return "c2c", str(row[0]), name
    if chat_type is None:
        fail(f"chat {peer} not found (neither a known group uin nor a friend uin/nt_uid)")
    return chat_type, peer, peer


def main() -> None:
    ap = argparse.ArgumentParser(description="Export one QQ chat to txt/json/csv (macOS)")
    ap.add_argument("--peer", required=True, help="group uin / friend uin / nt_uid")
    ap.add_argument("--chat-type", choices=["group", "c2c"], default=None,
                    help="auto-detected when omitted")
    ap.add_argument("--self-uin", default=os.environ.get("QQ_SELF_UIN", ""),
                    help="your own QQ number (or set QQ_SELF_UIN)")
    ap.add_argument("--out-dir", default=str(HOME / "qq-chat-export"))
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for the key")
    ap.add_argument("--keep", action="store_true",
                    help="keep the temp workdir (contains sensitive data!)")
    args = ap.parse_args()

    preflight()
    work = Path(f"/tmp/qq_export_{int(time.time())}")
    work.mkdir()
    staging = None
    try:
        nt_hash = find_nt_hash()
        app = resign_copy(work)
        staging = Staging(nt_hash)
        staging.build()
        staged_nt_db = QQDATA / nt_hash / "nt_db"
        salts = db_salts(staged_nt_db)
        key = extract_key(app, work, salts, args.timeout)
        plain = decrypt_all(work, staged_nt_db, key)

        chat_type, peer_resolved, chat_name = resolve_chat(plain, args.peer, args.chat_type)
        log(f"target chat: {chat_name} ({chat_type} {peer_resolved})")
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in peer_resolved)
        prefix = f"{chat_type}_{safe}"
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            "python3", str(EXPORTER),
            "--db-dir", str(plain), "--chat-type", chat_type, "--peer", peer_resolved,
            "--out-dir", str(out_dir), "--prefix", prefix,
        ]
        if args.self_uin:
            cmd += ["--self-uin", args.self_uin]
        r = run(cmd, timeout=600)
        print(r.stdout.strip())
        if r.returncode != 0:
            fail(f"exporter failed: {r.stderr.strip()}")
        log(f"exported -> {out_dir}/{prefix}_full.txt / .json / .csv (+summary/daily_counts)")
    finally:
        if staging is not None:
            staging.cleanup()
        if args.keep:
            log(f"--keep: workdir kept at {work} (contains sensitive data, delete after use)")
        else:
            shutil.rmtree(work, ignore_errors=True)
            log("workdir removed (key, plain DBs, re-signed copy all deleted)")


if __name__ == "__main__":
    main()
