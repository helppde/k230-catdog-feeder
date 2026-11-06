# -*- coding: utf-8 -*-
"""
PC 端服务：接收 K230 上传的照片 + 网页看统计

只用标准库（http.server + sqlite3 + json），不需要 pip install 任何东西。
图像处理用 Pillow（基本都自带），个体识别有 torch 就自动用，没有就退回手工特征。

启动：
    python pc/run.py
    浏览器打开 http://127.0.0.1:8000

接口：
    POST /api/v1/visit?device=&species=&conf=&box=&ws=&hs=&ts=   原始 JPEG 放 body
         （也支持 multipart/form-data，方便用 curl 手测）
    GET  /api/v1/overview            网页用的全部数据（一次拿全）
    GET  /api/v1/visits?limit=50     最近到访
    POST /api/v1/individual/<id>/rename   {"name": "小黑"}
    POST /api/v1/individual/<id>/merge    {"into": 3}
    POST /api/v1/visit/<id>/assign        {"individual_id": 3}
    GET  /api/v1/health
    GET  /img/<yyyyMMdd>/<file>.jpg
    GET  /                           网页
"""

import io
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from email.parser import BytesParser
from email.policy import default as email_default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image

# 让 `python pc/server.py` 和 `python -m pc.server` 都能跑
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config as PC          # noqa: E402
import db as dbmod           # noqa: E402
import identity              # noqa: E402

DB = None                    # 在 main() 里创建

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


# ================================================================
# 工具
# ================================================================
def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


def _safe_join(root, rel):
    """防 ../ 穿越：解析完必须还在 root 下面"""
    root = os.path.abspath(root)
    p = os.path.abspath(os.path.join(root, rel))
    if not p.startswith(root + os.sep) and p != root:
        return None
    return p


def _check_token(q):
    if not PC.API_TOKEN:
        return True
    return q.get("token", [""])[0] == PC.API_TOKEN


# ================================================================
# 上传处理
# ================================================================
def handle_visit(raw_body, ctype, query):
    """一张照片 -> 一次到访记录。返回要回给 K230 的 dict。"""
    now = time.time()
    t_recv = time.time()            # 收完图到回包的耗时，用于排查"客户端说没响应"

    query = dict(query)
    flat = {k: (v[0] if isinstance(v, list) and v else v) for k, v in query.items()}

    # multipart 兼容（curl 手测用）
    if ctype and ctype.startswith("multipart/form-data"):
        try:
            body, fields = _parse_multipart(raw_body, ctype)
        except Exception as e:
            return 400, {"ok": False, "error": "multipart 解析失败: %s" % e}
        flat.update(fields)
    else:
        body = raw_body

    if not body:
        return 400, {"ok": False, "error": "empty body"}
    if len(body) > PC.MAX_UPLOAD_BYTES:
        return 413, {"ok": False, "error": "image too large"}
    # JPEG 头 FF D8；也允许 PNG（有些固件 compress 出来是 PNG）
    if not (body[:2] == b"\xff\xd8" or body[:8] == b"\x89PNG\r\n\x1a\n"):
        return 400, {"ok": False, "error": "not a JPEG/PNG"}

    species = (flat.get("species") or "unknown").lower()
    if species not in ("cat", "dog"):
        species = "unknown"
    device = flat.get("device") or "k230"
    device_ts = flat.get("ts") or None
    box = flat.get("box") or None
    uid = flat.get("uid") or None
    backfill = str(flat.get("bf") or "0") in ("1", "true", "yes")
    try:
        conf = float(flat.get("conf")) if flat.get("conf") else None
    except Exception:
        conf = None

    # 补传的照片用**拍照时刻**入库，而不是"服务器收到它的时刻"。
    # 为什么：SD 卡里可能躺着一周前的照片，按收到时刻入库会让今天的统计
    # 突然多出几次到访，看上去像"网页数据对不上串口"。
    # 只信 2020 年以后的时间戳（板子没 RTC 时会报 0 或 1970）。
    if backfill and device_ts:
        try:
            t_dev = float(device_ts)
            if 1600000000 < t_dev < 4102444800:
                now = t_dev
        except Exception:
            pass

    # ---- 去重：同一张照片重传过就把老记录还给它 ----
    # K230 等响应有超时，超时后会重传同一张图（它也没办法知道服务端其实存成功了）。
    # 这里靠 uid 认出来，既不再存一份，也不做一次多余的特征提取 ——
    # 顺便让客户端这次能顺利拿到 200，不用再重试。
    if uid:
        old = DB.find_by_uid(uid)
        if old is not None:
            return 200, {
                "ok": True,
                "duplicate": True,
                "visit_id": old["id"],
                "individual_id": old["individual_id"],
                "species": old["species"],
                "name": None,
                "ts": old["ts"],
            }

    # ---- 落盘 ----
    day = time.strftime("%Y%m%d", time.localtime(now))
    folder = os.path.join(PC.IMG_DIR, day)
    os.makedirs(folder, exist_ok=True)
    name = uuid.uuid4().hex
    ext = ".jpg"
    full_path = os.path.join(folder, name + ext)
    thumb_path = os.path.join(folder, name + "_t" + ext)

    try:
        img = Image.open(io.BytesIO(body))
        img.load()
    except Exception as e:
        return 400, {"ok": False, "error": "decode failed: %s" % e}

    rgb = img.convert("RGB")
    try:
        rgb.save(full_path, "JPEG", quality=PC.JPEG_QUALITY)
        tw = min(PC.THUMB_WIDTH, rgb.width)
        th = max(1, int(rgb.height * tw / max(1, rgb.width)))
        rgb.resize((tw, th), Image.LANCZOS).save(thumb_path, "JPEG", quality=80)
    except Exception as e:
        return 500, {"ok": False, "error": "save failed: %s" % e}

    rel = "%s/%s%s" % (day, name, ext)
    rel_thumb = "%s/%s_t%s" % (day, name, ext)

    # ---- 认个体 ----
    vec = None
    try:
        vec = identity.EMBEDDER.embed(rgb)
    except Exception as e:
        print("[visit] 特征提取异常: %s" % e)

    individual_id = None
    sim = None
    is_new = False
    if vec is not None:
        individual_id, sim = identity.best_match(DB, species, vec)
        if sim is not None and sim < 0:
            sim = None          # 库里还没有同类个体，没有可比的对象
        if individual_id is None:
            individual_id = DB.create_individual(species)
            is_new = True
        DB.add_embedding(individual_id, vec)

    # ---- 入库 ----
    vid = DB.add_visit(species=species, ts=now, individual_id=individual_id,
                       confidence=conf, box=box, image=rel, thumb=rel_thumb,
                       sim=sim, device=device, device_ts=device_ts,
                       nbytes=len(body), uid=uid, backfill=backfill)

    ind = DB.get_individual(individual_id) if individual_id else None
    visits = DB.count_visits(individual_id) if individual_id else 0

    out = {
        "ok": True,
        "visit_id": vid,
        "individual_id": individual_id,
        "name": ind["name"] if ind else None,
        "species": species,
        "sim": round(sim, 4) if sim is not None else None,
        "is_new": is_new,
        "visits": visits,
        "ts": now,
    }
    print("[visit] %s %s -> %s (sim=%s, %s)%s %.0f ms%s"
          % (device, species,
             out["name"] or "未识别",
             "%.3f" % sim if sim is not None else "--",
             "新个体" if is_new else "老朋友",
             " [补传]" if backfill else "",
             (time.time() - t_recv) * 1000.0,
             "  uid=%s" % uid if uid else ""))
    return 200, out


def _parse_multipart(raw, ctype):
    """用标准库 email 模块拆 multipart/form-data（只为 curl 手测方便）"""
    header = ("Content-Type: %s\r\nMIME-Version: 1.0\r\n\r\n" % ctype).encode()
    msg = BytesParser(policy=email_default).parsebytes(header + raw)
    body = b""
    fields = {}
    if not msg.is_multipart():
        raise ValueError("not multipart")
    for part in msg.iter_parts():
        cdisp = part.get("Content-Disposition", "")
        m = re.search(r'name="([^"]+)"', cdisp)
        fname = re.search(r'filename="([^"]*)"', cdisp)
        payload = part.get_payload(decode=True) or b""
        if fname:
            body = payload
        elif m:
            try:
                fields[m.group(1)] = payload.decode("utf-8", "replace")
            except Exception:
                pass
    return body, fields


# ================================================================
# HTTP
# ================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "CatDogFeeder/1.0"
    protocol_version = "HTTP/1.0"     # 简单：一问一答就关，K230 的裸 socket 也好对接

    # 读请求的超时（秒）。
    # 为什么要有：K230 发图发到一半断线（WiFi 抖动、板子重启）时，
    # self.rfile.read(n) 会永远等下去，那条线程就一直占着不放。
    # 有了超时，这种半截连接最多赖 60 秒就自己散掉。
    timeout = 60

    # ---------------- 输出 ----------------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, _json_bytes(obj))

    # ---------------- 读 body ----------------
    def _drain_body(self, limit=None):
        """把这次请求的 body 读掉并丢掉。

        为什么非要有这一步：
            拒绝请求时（token 不对 / 太大）如果**不读 body 就关连接**，客户端
            还在往里写（360KB 的照片要写一会儿），内核会回一个 RST。于是客户端
            看到的是 ECONNRESET，而不是我们精心写的那句 "bad token" ——
            提示全白费，它还会以为是网络问题，傻傻重试三次。
            先读干净再回响应，客户端就能正常收到 403。

        读不出来 / 太大也不能卡住，所以有上限且全程吞异常。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return
        cap = limit if limit is not None else PC.MAX_UPLOAD_BYTES
        if n > cap:
            # 太大就别真读（会占内存），但也不能立刻关 —— 告诉 http.server
            # 这条连接用完就关，剩下的数据交给内核丢弃。
            self.close_connection = True
            return
        try:
            self.rfile.read(n)
        except Exception:
            pass

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        if n > PC.MAX_UPLOAD_BYTES:
            return b""
        return self.rfile.read(n)

    # ---------------- 路由 ----------------
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        try:
            if path == "/api/v1/health":
                return self._json(200, {
                    "ok": True,
                    "backend": identity.EMBEDDER.backend or "loading",
                    "threshold": PC.MATCH_THRESHOLD,
                    "db": PC.DB_PATH,
                })

            if path == "/api/v1/overview" and method == "GET":
                return self._overview(query)

            if path == "/api/v1/visits" and method == "GET":
                limit = int(query.get("limit", ["50"])[0])
                iid = query.get("individual_id", [None])[0]
                rows = DB.recent_visits(min(limit, 500),
                                        int(iid) if iid else None)
                return self._json(200, {"ok": True, "visits": rows})

            if path == "/api/v1/visit" and method == "POST":
                if not _check_token(query):
                    # 先把 body 读掉再回 403，否则客户端只看到 ECONNRESET
                    self._drain_body()
                    return self._json(403, {"ok": False, "error": "bad token"})
                ctype = self.headers.get("Content-Type") or ""
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n <= 0:
                    return self._json(400, {"ok": False, "error": "empty body"})
                # multipart 还要带边界和元数据，留 64KB 余量
                if n > PC.MAX_UPLOAD_BYTES + 65536:
                    self.close_connection = True
                    return self._json(413, {"ok": False, "error": "too large"})
                body = self.rfile.read(n)
                code, obj = handle_visit(body, ctype, query)
                return self._json(code, obj)

            m = re.match(r"^/api/v1/individual/(\d+)/rename$", path)
            if m and method == "POST":
                data = self._read_json()
                name = (data.get("name") or "").strip()
                if not name:
                    return self._json(400, {"ok": False, "error": "name required"})
                DB.rename_individual(int(m.group(1)), name[:40])
                return self._json(200, {"ok": True})

            m = re.match(r"^/api/v1/individual/(\d+)/merge$", path)
            if m and method == "POST":
                data = self._read_json()
                into = data.get("into")
                if into is None:
                    return self._json(400, {"ok": False, "error": "into required"})
                n = DB.merge_individuals(int(m.group(1)), int(into))
                return self._json(200, {"ok": True, "moved": n})

            m = re.match(r"^/api/v1/individual/(\d+)/delete$", path)
            if m and method == "POST":
                DB.delete_individual(int(m.group(1)))
                return self._json(200, {"ok": True})

            m = re.match(r"^/api/v1/visit/(\d+)/assign$", path)
            if m and method == "POST":
                data = self._read_json()
                iid = data.get("individual_id")
                ok = DB.assign_visit(int(m.group(1)), int(iid) if iid else None)
                return self._json(200 if ok else 404, {"ok": ok})

            if path.startswith("/img/"):
                rel = path[len("/img/"):]
                if not rel.lower().endswith((".jpg", ".jpeg", ".png")):
                    return self._send(404, b"not found", "text/plain")
                p = _safe_join(PC.IMG_DIR, rel)
                if not p or not os.path.isfile(p):
                    return self._send(404, b"missing", "text/plain")
                with open(p, "rb") as f:
                    data = f.read()
                return self._send(200, data,
                                  _MIME.get(os.path.splitext(p)[1].lower(), "image/jpeg"),
                                  {"Cache-Control": "public, max-age=86400"})

            if path in ("/", "/index.html"):
                return self._static("index.html")

            return self._static(path.lstrip("/"))

        except Exception:
            traceback.print_exc()
            try:
                self._json(500, {"ok": False, "error": "internal error"})
            except Exception:
                pass

    # ---------------- 各接口 ----------------
    def _overview(self, query):
        ov = DB.overview()
        ov["individuals"] = DB.list_individuals()
        ov["recent"] = DB.recent_visits(30)
        ov["config"] = {
            "threshold": PC.MATCH_THRESHOLD,
            "backend": identity.EMBEDDER.backend or "loading",
            "species_names": PC.SPECIES_NAMES,
        }
        self._json(200, ov)

    def _read_json(self):
        body = self._read_body()
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {}

    def _static(self, rel):
        p = _safe_join(PC.WEB_DIR, rel)
        if not p or not os.path.isfile(p):
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        with open(p, "rb") as f:
            data = f.read()
        ctype = _MIME.get(os.path.splitext(p)[1].lower(), "application/octet-stream")
        self._send(200, data, ctype)

    # 关掉默认的每请求一行日志，太吵（上传我们自己会打）
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        pass


# ================================================================
# 启动
# ================================================================
def create_server(host=None, port=None, db_path=None):
    global DB
    os.makedirs(PC.DATA_DIR, exist_ok=True)
    os.makedirs(PC.IMG_DIR, exist_ok=True)
    DB = dbmod.DB(db_path or PC.DB_PATH)
    identity.EMBEDDER.warmup()
    srv = ThreadingHTTPServer((host or PC.HOST, port or PC.PORT), Handler)
    srv.daemon_threads = True

    # 有证书就自己开 TLS（没有反代时用；有 nginx/Caddy 就别开，重复加密没意义）
    if PC.TLS_CERT and PC.TLS_KEY:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(PC.TLS_CERT, PC.TLS_KEY)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        srv.is_tls = True
    else:
        srv.is_tls = False
    return srv


def _lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    srv = create_server()
    host, port = srv.server_address[0], srv.server_address[1]
    ip = _lan_ip()
    scheme = "https" if getattr(srv, "is_tls", False) else "http"
    print("=" * 62)
    print(" 流浪猫狗放饭 · PC 服务端")
    print("=" * 62)
    print(" 本机访问 : %s://127.0.0.1:%d" % (scheme, port))
    print(" 局域网   : %s://%s:%d" % (scheme, ip, port))
    print(" 数据目录 : %s" % PC.DATA_DIR)
    print("-" * 62)
    if getattr(srv, "is_tls", False):
        print(" 已启用 HTTPS（本服务自己做的 TLS）")
    elif PC.TLS_CERT:
        print(" 提示：只配了证书的一半，TLS 没生效（要 CERT 和 KEY 都有）")
    print(" K230 的 k230_trigger.py 里填：")
    print("     SERVER_URL = \"%s://%s:%d\"" % (scheme, ip, port))
    if not PC.API_TOKEN:
        print("")
        print(" ⚠️  API_TOKEN 是空的：本机谁都能往库里塞照片。")
        print("     只在纯局域网调试时这样；放到公网前必须设置：")
        print("     set CATDOG_TOKEN=一串随机字符")
    if host in ("0.0.0.0", ""):
        print("")
        print(" 放到云端时：K230 连的是云服务器的公网地址，不是上面这个局域网 IP。")
        print(" 例：SERVER_URL = \"https://你的域名\"（前面挂 nginx/Caddy 做 TLS，推荐）")
    print("=" * 62)
    print(" Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在退出...")
    finally:
        srv.server_close()
        if DB is not None:
            DB.close()
        print("已停止")


if __name__ == "__main__":
    main()
