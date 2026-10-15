#!/usr/bin/env python
"""Pull finished images off the Seestar's SMB share (//<scope>/EMMC Images/).

The scope answers SMB as a guest with signing and encryption off, which the
high-level smbclient wrapper refuses, so this talks to smbprotocol directly.
"""
import os, sys, uuid
from pathlib import Path
from smbprotocol.connection import Connection
from smbprotocol.session import Session
from smbprotocol.tree import TreeConnect
from smbprotocol.open import (Open, CreateDisposition, CreateOptions, FileAttributes,
                              ShareAccess, ImpersonationLevel, DirectoryAccessMask,
                              FilePipePrinterAccessMask, FileInformationClass)

import config
IP = config.require_ip()
SHARE = "EMMC Images"

def connect():
    c = Connection(uuid.uuid4(), IP, 445, require_signing=False); c.connect()
    s = Session(c, username="guest", password="", require_encryption=False); s.connect()
    t = TreeConnect(s, rf"\\{IP}\{SHARE}"); t.connect(require_secure_negotiate=False)
    return c, s, t

def listdir(tree, path):
    d = Open(tree, path)
    d.create(ImpersonationLevel.Impersonation, DirectoryAccessMask.FILE_LIST_DIRECTORY,
             FileAttributes.FILE_ATTRIBUTE_DIRECTORY, ShareAccess.FILE_SHARE_READ,
             CreateDisposition.FILE_OPEN, CreateOptions.FILE_DIRECTORY_FILE)
    out = []
    # One QUERY_DIRECTORY call returns only as many entries as fit in its buffer (~300 frame files);
    # keep asking until the server says there are no more, or big *_sub folders get listed partially.
    while True:
        try:
            batch = d.query_directory("*", FileInformationClass.FILE_DIRECTORY_INFORMATION, max_output=1048576)
        except Exception as ex:
            if "NO_MORE_FILES" in str(ex).upper() or "0x80000006" in str(ex) or "NoMoreFiles" in type(ex).__name__:
                break
            raise
        if not batch:
            break
        for e in batch:
            name = e['file_name'].get_value().decode('utf-16-le')
            if name in (".", ".."):
                continue
            attrs = e['file_attributes'].get_value()
            out.append((name,
                        bool(attrs & FileAttributes.FILE_ATTRIBUTE_DIRECTORY),
                        e['end_of_file'].get_value(),
                        e['last_write_time'].get_value()))
    d.close()
    return out

def fetch(tree, path, dest: Path):
    f = Open(tree, path)
    f.create(ImpersonationLevel.Impersonation, FilePipePrinterAccessMask.FILE_READ_DATA,
             FileAttributes.FILE_ATTRIBUTE_NORMAL, ShareAccess.FILE_SHARE_READ,
             CreateDisposition.FILE_OPEN, CreateOptions.FILE_NON_DIRECTORY_FILE)
    dest.parent.mkdir(parents=True, exist_ok=True)
    off, chunk = 0, 1 << 20
    with open(dest, "wb") as fh:
        while True:
            try:
                b = f.read(off, chunk)
            except Exception:
                break
            if not b:
                break
            fh.write(b); off += len(b)
    f.close()
    return off

def walk(tree, path=""):
    for name, isdir, size, mtime in listdir(tree, path):
        p = f"{path}\\{name}" if path else name
        if isdir:
            yield from walk(tree, p)
        else:
            yield p, size, mtime

if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    c, s, t = connect()
    total = 0
    for p, size, mtime in walk(t, "MyWorks"):
        dest = out / p.replace("\\", "/")
        if dest.exists() and dest.stat().st_size == size:
            print(f"  skip {p}"); continue
        n = fetch(t, p, dest)
        os.utime(dest, (mtime.timestamp(), mtime.timestamp()))
        total += n
        print(f"  {p}  {n/1e6:.1f} MB  {mtime:%Y-%m-%d %H:%M}")
    print(f"pulled {total/1e6:.1f} MB")
    t.disconnect(); s.disconnect(); c.disconnect()
