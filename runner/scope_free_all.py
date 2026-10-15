"""Free space ON THE SCOPE: delete every file on the scope whose local mirror copy (written by `seestar pull`)
exists with the same size, so the scope never fills up mid-night. Files without a matching local copy are kept and
reported; folders written in the last 30 minutes are skipped (the scope may still be using them). Never touches the
local copies. DRY=1 to only report."""
import sys, os
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'seestar'))
import config
import scope_pull as S
from smbprotocol.open import (Open, CreateDisposition, CreateOptions, FileAttributes, ShareAccess,
                              ImpersonationLevel, FilePipePrinterAccessMask, DirectoryAccessMask)
DRY = os.environ.get("DRY") == "1"
OUT = config.CAPTURES / 'scope'          # where `seestar pull` mirrors the scope
LO, HI = datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2100, 1, 1, tzinfo=timezone.utc)   # all folders
c, s, t = S.connect()
def delete(path, is_dir=False):
    o = Open(t, path)
    o.create(ImpersonationLevel.Impersonation, FilePipePrinterAccessMask.DELETE | FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
             FileAttributes.FILE_ATTRIBUTE_DIRECTORY if is_dir else FileAttributes.FILE_ATTRIBUTE_NORMAL,
             ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE | ShareAccess.FILE_SHARE_DELETE,
             CreateDisposition.FILE_OPEN,
             (CreateOptions.FILE_DIRECTORY_FILE if is_dir else CreateOptions.FILE_NON_DIRECTORY_FILE) | CreateOptions.FILE_DELETE_ON_CLOSE)
    o.close()
from datetime import timedelta
RECENT = datetime.now(timezone.utc) - timedelta(minutes=30)   # skip folders the scope may still be writing
freed = kept = 0; folders = []
for name, isd, sz, m in S.listdir(t, "MyWorks"):
    if not isd:
        continue
    files = [(p, size, mt) for p, size, mt in S.walk(t, f"MyWorks\\{name}")]
    if not files or max(mt for _, _, mt in files) >= HI or min(mt for _, _, mt in files) < LO or max(mt for _, _, mt in files) >= RECENT:
        continue
    fb = 0; fk = 0
    for p, size, mt in files:
        local = OUT / p.replace("\\", "/")
        if local.exists() and local.stat().st_size == size:
            if not DRY: delete(p)
            fb += size
        else:
            fk += 1
    if fk == 0 and not DRY:
        try: delete(f"MyWorks\\{name}", is_dir=True)
        except Exception as ex: print("  folder not removed:", name, str(ex)[:60])
    folders.append(name); freed += fb; kept += fk
    print(f"{'would free' if DRY else 'freed'} {fb/1e9:5.2f} GB  {name}  (kept {fk} uncopied)", flush=True)
print(f"TOTAL {'would free' if DRY else 'freed'} {freed/1e9:.1f} GB from {len(folders)} folders; kept {kept} files not on the laptop", flush=True)
