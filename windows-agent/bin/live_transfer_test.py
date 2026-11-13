"""Live file-transfer test: uploads/downloads/verifies against the real Windows Agent (needs 0.3.0+) from the server through the tunnel.

Only creates winpc-xfer-*.bin test files in the remote directory you specify (there is no delete tool; delete them yourself afterwards).
Usage:
    WINPC_TOKEN=... python live_transfer_test.py 'C:\\Users\\me\\Desktop\\winpc-transfer-test\\' [url]
"""
import hashlib
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
spec = importlib.util.spec_from_file_location("winpc_cp", HERE.parents[2] / "claude-plugin/winpc/scripts/winpc_cp.py")
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

if len(sys.argv) < 2 or not sys.argv[1].endswith(("\\", "/")):
    sys.exit("Usage: live_transfer_test.py '<remote directory, ending with \\>' [url]")
REMOTE_DIR = sys.argv[1]
URL = sys.argv[2] if len(sys.argv) > 2 else cp.DEFAULT_URL
sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
results = []


def check(name, fn):
    t = time.time()
    try:
        detail = fn() or ""
        results.append((name, True, f"{detail} ({time.time() - t:.1f}s)"))
    except Exception as e:  # noqa: BLE001
        results.append((name, False, f"{type(e).__name__}: {e}"[:160]))


def expect_error(fn, needle):
    try:
        fn()
    except cp.TransferError as e:
        assert needle in str(e), f"unexpected error message: {e}"
        return str(e)[:60]
    raise AssertionError("should have been refused, but it succeeded")


c = cp.McpClient(URL, os.environ["WINPC_TOKEN"])
import atexit
atexit.register(c.close)     # close the MCP session at exit
tmp = Path(tempfile.mkdtemp())
tag = f"winpc-xfer-{int(time.time())}"

binary = os.urandom(3 * 1024 * 1024 + 321) + b"\x00\xff\xfe"                 # 3 chunks + a remainder, with NUL / non-UTF-8
crlf = ("\u7b2c\u4e00\u884c\r\n\u7b2c\u4e8c\u884c emoji \U0001f642\r\nline3\r\n").encode("utf-8")              # the text tools normalize CRLF to LF; here it must be preserved
(tmp / "bin").write_bytes(binary)
(tmp / "crlf").write_bytes(crlf)
rb, rc = f"{REMOTE_DIR}{tag}.bin", f"{REMOTE_DIR}{tag}-crlf.txt"

check("upload 3 MiB binary", lambda: str(cp.put(c, str(tmp / "bin"), rb, quiet=True)["size"]) + " bytes")
check("upload CRLF text (byte-exact)", lambda: str(cp.put(c, str(tmp / "crlf"), rc, quiet=True)["size"]) + " bytes")
check("stat + sha256", lambda: "" if c.call("stat_file", path=rb, sha256=True)["sha256"] == sha(binary) else 1 / 0)


def dl(remote, expect, name):
    out = tmp / name
    cp.get(c, remote, str(out), quiet=True, chunk=700_000)
    assert out.read_bytes() == expect, "downloaded content differs from the original"
    return "byte-identical"


check("download binary and compare", lambda: dl(rb, binary, "bin.back"))
check("download CRLF text and compare", lambda: dl(rc, crlf, "crlf.back"))
check("overwrite protection (must refuse without overwrite)", lambda: expect_error(lambda: cp.put(c, str(tmp / "bin"), rb, quiet=True), "already exists"))
check("overwrite succeeds and creates a backup", lambda: "backup: " + str(cp.put(c, str(tmp / "crlf"), rb, overwrite=True, quiet=True).get("backup")))
check(".exe upload is blocked", lambda: expect_error(lambda: cp.put(c, str(tmp / "bin"), f"{REMOTE_DIR}{tag}.exe", quiet=True), "not allowed"))

w = max(len(n) for n, *_ in results)
for n, ok, info in results:
    print(f"{'PASS' if ok else 'FAIL'}  {n.ljust(w)}  {info}")
bad = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f", failed: {bad}" if bad else ""))
print(f"Test files remain in {REMOTE_DIR} ({tag}*); please delete them yourself.")
sys.exit(1 if bad else 0)
