"""实机文件传输测试：在服务器上经隧道对真实 Windows Agent（需 0.3.0+）做上传/下载/校验。

只在你指定的远端目录里创建 winpc-xfer-*.bin 测试文件（没有删除工具，测完请自行删除）。
用法：
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
    sys.exit("用法: live_transfer_test.py '<远端目录，以 \\ 结尾>' [url]")
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
        assert needle in str(e), f"错误信息不符: {e}"
        return str(e)[:60]
    raise AssertionError("本应被拒绝，但成功了")


c = cp.McpClient(URL, os.environ["WINPC_TOKEN"])
import atexit
atexit.register(c.close)     # 结束时关闭 MCP 会话
tmp = Path(tempfile.mkdtemp())
tag = f"winpc-xfer-{int(time.time())}"

binary = os.urandom(3 * 1024 * 1024 + 321) + b"\x00\xff\xfe"                 # 3 块 + 零头，含 NUL / 非 UTF-8
crlf = ("第一行\r\n第二行 emoji 🙂\r\nline3\r\n").encode("utf-8")              # 文本工具会把 CRLF 规范成 LF，这里必须保持
(tmp / "bin").write_bytes(binary)
(tmp / "crlf").write_bytes(crlf)
rb, rc = f"{REMOTE_DIR}{tag}.bin", f"{REMOTE_DIR}{tag}-crlf.txt"

check("上传 3MiB 二进制", lambda: str(cp.put(c, str(tmp / "bin"), rb, quiet=True)["size"]) + " 字节")
check("上传 CRLF 文本（字节级）", lambda: str(cp.put(c, str(tmp / "crlf"), rc, quiet=True)["size"]) + " 字节")
check("stat + sha256", lambda: "" if c.call("stat_file", path=rb, sha256=True)["sha256"] == sha(binary) else 1 / 0)


def dl(remote, expect, name):
    out = tmp / name
    cp.get(c, remote, str(out), quiet=True, chunk=700_000)
    assert out.read_bytes() == expect, "下载内容与原文件不一致"
    return "逐字节一致"


check("下载二进制并比对", lambda: dl(rb, binary, "bin.back"))
check("下载 CRLF 文本并比对", lambda: dl(rc, crlf, "crlf.back"))
check("覆盖保护(不加 overwrite 应拒绝)", lambda: expect_error(lambda: cp.put(c, str(tmp / "bin"), rb, quiet=True), "已存在"))
check("overwrite 成功并产生备份", lambda: "备份: " + str(cp.put(c, str(tmp / "crlf"), rb, overwrite=True, quiet=True).get("backup")))
check(".exe 上传被拦截", lambda: expect_error(lambda: cp.put(c, str(tmp / "bin"), f"{REMOTE_DIR}{tag}.exe", quiet=True), "不允许写入"))

w = max(len(n) for n, *_ in results)
for n, ok, info in results:
    print(f"{'PASS' if ok else 'FAIL'}  {n.ljust(w)}  {info}")
bad = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} 通过" + (f"，失败: {bad}" if bad else ""))
print(f"测试文件留在 {REMOTE_DIR}（{tag}*），请自行删除。")
sys.exit(1 if bad else 0)
