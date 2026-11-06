# -*- coding: utf-8 -*-
"""
启动 PC 服务端

    python pc/run.py                 # 默认 0.0.0.0:8000
    python pc/run.py --port 9000
    python pc/run.py --host 127.0.0.1
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config as PC          # noqa: E402
import server                # noqa: E402


class _Tee(object):
    """把 stdout 同时写到日志文件。

    为什么需要它：控制台一关（或者用 pythonw / 计划任务跑），
    "[visit] ..." 这些行就没了 —— 而那恰恰是排查"串口显示上传成功、
    网页却没东西"唯一的对照依据。

    现在每条到访都会落进 pc/data/server.log，可以和 K230 串口日志
    逐行对照：串口里每次 "上传成功" 都应该在 server.log 里找到一条
    "[visit] ... uid=..."。
    """

    def __init__(self, stream, path, encoding="utf-8"):
        self.stream = stream
        self.path = path
        # 太大就轮转一次，别让日志无限长
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 2 * 1024 * 1024:
                bak = path + ".1"
                if os.path.isfile(bak):
                    os.remove(bak)
                os.replace(path, bak)
        except Exception:
            pass
        try:
            self.fh = open(path, "a", encoding=encoding, errors="replace")
        except Exception:
            self.fh = None

    def write(self, data):
        if not isinstance(data, str):
            data = str(data)
        # 先写文件、再写控制台：万一控制台被 Windows 的"选择模式"冻住，
        # 日志文件里至少是完整的，事后还能查。
        if self.fh is not None:
            try:
                self.fh.write(data)
                if "\n" in data:
                    self.fh.flush()
            except Exception:
                pass
        try:
            if self.stream is not None:
                self.stream.write(data)
        except Exception:
            pass
        return len(data)

    def flush(self):
        for obj in (self.stream, self.fh):
            try:
                if obj is not None:
                    obj.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return bool(self.stream is not None and self.stream.isatty())
        except Exception:
            return False

    def close(self):
        self.flush()
        try:
            if self.fh is not None:
                self.fh.close()
        except Exception:
            pass


def _disable_quickedit():
    """关掉控制台的"快速编辑"模式。

    这是 Windows 上一个很坑的行为：只要有人在服务端窗口里点一下、拖选一段
    文字，控制台就进入选择模式，**下一次 print() 会永久阻塞**，整个进程
    连同 HTTP 服务一起卡死 —— 表现就是"日志停住、网页转圈、板子超时"，
    看起来完全像代码 bug。

    关掉之后鼠标点选不再冻结进程。要复制日志，看 pc/data/server.log。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.GetStdHandle(-11)                       # STD_OUTPUT_HANDLE
        if h in (0, -1, None):
            return
        mode = wintypes.DWORD()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return
        ENABLE_QUICK_EDIT = 0x0040
        ENABLE_EXTENDED = 0x0080
        new = (mode.value | ENABLE_EXTENDED) & ~ENABLE_QUICK_EDIT
        k32.SetConsoleMode(h, new)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="流浪猫狗放饭 · PC 服务端")
    ap.add_argument("--host", default=PC.HOST)
    ap.add_argument("--port", type=int, default=PC.PORT)
    ap.add_argument("--db", default=PC.DB_PATH)
    ap.add_argument("--log", default=os.path.join(PC.DATA_DIR, "server.log"),
                    help="日志文件（默认 pc/data/server.log，传空字符串关闭）")
    args = ap.parse_args()

    _disable_quickedit()

    tee = None
    if args.log:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        except Exception:
            pass
        tee = _Tee(sys.stdout, args.log)
        sys.stdout = tee
        sys.stderr = tee

    srv = server.create_server(args.host, args.port, args.db)
    port = srv.server_address[1]
    scheme = "https" if getattr(srv, "is_tls", False) else "http"
    ip = server._lan_ip()
    print("=" * 62)
    print(" 流浪猫狗放饭 · PC 服务端")
    print("=" * 62)
    print(" 本机访问 : %s://127.0.0.1:%d" % (scheme, port))
    print(" 局域网   : %s://%s:%d" % (scheme, ip, port))
    print(" 数据目录 : %s" % PC.DATA_DIR)
    if args.log:
        print(" 日志文件 : %s" % os.path.abspath(args.log))
    print("-" * 62)
    print(" 把 K230 的 k230_trigger.py 改成：")
    print("     SERVER_URL = \"%s://%s:%d\"" % (scheme, ip, port))
    if not PC.API_TOKEN:
        print("")
        print(" ⚠️  API_TOKEN 为空（谁都能传照片）。放公网前必须设置 CATDOG_TOKEN。")
    print("")
    print(" 部署到云端时，K230 填公网地址而不是上面的局域网 IP，例如：")
    print("     SERVER_URL = \"https://你的域名\"")
    print("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中...")
    finally:
        srv.server_close()
        if server.DB is not None:
            server.DB.close()
        if tee is not None:
            sys.stdout = tee.stream
            sys.stderr = tee.stream
            tee.close()


if __name__ == "__main__":
    main()
