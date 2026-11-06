# -*- coding: utf-8 -*-
"""
把文件推到 K230 的 /sdcard（走 USB 串口 REPL，不需要拔卡）

为什么要这个：
    模型的 kmodel 有 3~7MB，CanMV IDE 的拖拽偶尔会中断（拷一半的文件加载
    就是 "Kmodel file not exist" 或者加载到一半崩）。这个脚本走 raw REPL，
    分块 base64 传输，**每块都校验**，断了能重来，还能顺便列目录。

用法（Windows，板子插 USB 后）：
    python tools/push_to_k230.py --port COM23 --ls
    python tools/push_to_k230.py --port COM23 --put C:\\path\\custom.kmodel /sdcard/kmodel/

要注意的：
    - **CanMV IDE 开着会占住串口**，先点 IDE 的「断开连接」或者关掉 IDE。
    - 传的过程中板子会闪电灯/无响应，正常，别拔线。
    - 目标目录不存在会自动 mkdir。
"""

import argparse
import base64
import os
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("缺 pyserial：python -m pip install pyserial")

CHUNK = 2048          # 每块原始字节数。太大 REPL 会截断长行，太小传得慢
BATCH = 8             # 一条命令里塞几块，减少往返次数


class Board(object):
    def __init__(self, port, baud=115200, timeout=0.3):
        self.s = serial.Serial(port, baud, timeout=timeout)
        self._raw = False

    def close(self):
        try:
            self.exit_raw()
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass

    # ---------- REPL 进出 ----------
    def enter_raw(self):
        if self._raw:
            return
        self.s.write(b"\r\x03")            # Ctrl-C：打断正在跑的程序
        time.sleep(0.3)
        self.s.reset_input_buffer()
        self.s.write(b"\x01")              # Ctrl-A：raw REPL
        got = self._read_until([b"raw REPL", b">"], 8)
        if b"raw REPL" not in got:
            raise IOError("进不了 raw REPL（收到 %r）。板子可能在跑死循环，"
                          "或者这个口不是 K230 的 REPL" % got[-120:])
        self._raw = True

    def exit_raw(self):
        if self._raw:
            self.s.write(b"\x02")          # Ctrl-B：退出 raw REPL
            self._raw = False

    def _read_until(self, marks, timeout):
        buf = b""
        t0 = time.time()
        while time.time() - t0 < timeout:
            ch = self.s.read(self.s.in_waiting or 1)
            if ch:
                buf += ch
                for m in marks:
                    if m in buf:
                        return buf
                t0 = time.time()
        return buf

    # ---------- 执行一段代码 ----------
    def exec(self, code, timeout=30):
        """在板子上跑一段 Python，返回 (stdout, stderr)。

        raw REPL 的回复格式是：
            stdout \x04 stderr \x04 >
        所以按 \x04 切开就是两个流，这点比猜日志可靠。
        """
        self.enter_raw()
        if isinstance(code, str):
            code = code.encode("utf-8")
        self.s.write(code + b"\x04")
        raw = self._read_until([b"\x04\x04>", b"\x04>"], timeout)
        body = raw
        if body.endswith(b">"):
            body = body[:-1]
        parts = body.split(b"\x04")
        out = parts[0] if parts else b""
        err = parts[1] if len(parts) > 1 else b""
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def run_ok(self, code, timeout=30):
        out, err = self.exec(code, timeout)
        if err.strip():
            raise IOError("板子执行出错:\n%s" % err.strip())
        return out

    # ---------- 常用操作 ----------
    def ls(self, path="/sdcard"):
        # 注意：这段板子代码里本身有 '%' 和引号，所以**不能**用 % 或 format
        # 去拼路径 —— 用 repr() 直接插进去，避免二次转义踩坑。
        code = (
            "import os\n"
            "p = " + repr(path) + "\n"
            "for n in sorted(os.listdir(p)):\n"
            "    q = p.rstrip('/') + '/' + n\n"
            "    try:\n"
            "        st = os.stat(q)\n"
            "        if (st[0] & 0x4000) != 0:\n"
            "            print('DIR  ' + n)\n"
            "        else:\n"
            "            print('     ' + n + '   ' + str(st[6]))\n"
            "    except Exception as e:\n"
            "        print('?    ' + n + '   ' + str(e))\n"
        )
        return self.run_ok(code)

    def mkdirs(self, path):
        code = (
            "import os\n"
            "p = " + repr(path) + "\n"
            "cur = ''\n"
            "for part in p.strip('/').split('/'):\n"
            "    cur += '/' + part\n"
            "    try:\n"
            "        os.mkdir(cur)\n"
            "    except OSError:\n"
            "        pass\n"
            "print('ok')\n"
        )
        self.run_ok(code)

    def size_of(self, path):
        code = (
            "import os\n"
            "try:\n"
            "    print(os.stat(" + repr(path) + ")[6])\n"
            "except Exception:\n"
            "    print(-1)\n"
        )
        out = self.run_ok(code)
        try:
            return int(out.strip().splitlines()[-1])
        except Exception:
            return -1

    def remove(self, path):
        self.exec("import os\ntry:\n    os.remove(" + repr(path) + ")\nexcept Exception:\n    pass\n")

    def put(self, local, remote, verbose=True):
        total = os.path.getsize(local)
        name = os.path.basename(remote)

        if self.size_of(remote) == total:
            if verbose:
                print("  已存在且大小一致，跳过: %s (%d 字节)" % (remote, total))
            return

        self.run_ok("f = open(" + repr(remote) + ", 'wb')\nprint('opened')\n")

        sent = 0
        t0 = time.time()
        with open(local, "rb") as fh:
            while True:
                chunks = []
                for _ in range(BATCH):
                    blob = fh.read(CHUNK)
                    if not blob:
                        break
                    chunks.append(base64.b64encode(blob))
                if not chunks:
                    break
                code = "import ubinascii\n" + "".join(
                    "f.write(ubinascii.a2b_base64(" + repr(c.decode("ascii")) + "))\n"
                    for c in chunks)
                self.run_ok(code, timeout=30)
                sent += sum(len(c) * 3 // 4 for c in chunks)
                if verbose:
                    pct = 100.0 * sent / total
                    speed = sent / max(0.001, time.time() - t0) / 1024.0
                    sys.stdout.write("\r  %s  %5.1f%%  %.0f KB/s  %d/%d"
                                     % (name, pct, speed, sent, total))
                    sys.stdout.flush()
        self.run_ok("f.close()\nprint('closed')\n")
        if verbose:
            print("")

        got = self.size_of(remote)
        if got != total:
            raise IOError("大小不对：板上 %d，本地 %d（传输被截断了，重跑一次）"
                          % (got, total))
        if verbose:
            print("  ✓ %s  %d 字节  校验通过" % (remote, got))


def main():
    ap = argparse.ArgumentParser(description="把文件推到 K230 /sdcard")
    ap.add_argument("--port", required=True, help="串口号，例如 COM23")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--ls", action="store_true", help="列 /sdcard")
    ap.add_argument("--ls-path", default="/sdcard", help="配合 --ls 指定目录")
    ap.add_argument("--put", nargs=2, metavar=("本地文件", "板子目录或路径"),
                    help="上传一个文件")
    ap.add_argument("--rm", metavar="板子路径", help="删一个文件")
    args = ap.parse_args()

    b = Board(args.port, args.baud)
    try:
        print("[board] 连接 %s ..." % args.port)
        b.enter_raw()
        print("[board] raw REPL OK")

        if args.rm:
            b.remove(args.rm)
            print("[board] 已删除 %s" % args.rm)

        if args.ls:
            print("\n--- %s ---" % args.ls_path)
            print(b.ls(args.ls_path))

        if args.put:
            local, remote = args.put
            if not os.path.isfile(local):
                sys.exit("本地文件不存在: %s" % local)
            if remote.endswith("/") or "." not in os.path.basename(remote):
                remote = remote.rstrip("/") + "/" + os.path.basename(local)
            d = remote.rsplit("/", 1)[0]
            b.mkdirs(d)
            print("[board] 上传 %s -> %s" % (local, remote))
            b.put(local, remote)
    finally:
        b.close()


if __name__ == "__main__":
    main()
