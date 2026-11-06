# -*- coding: utf-8 -*-
"""
K230 → 云端连通性自检：从板子这边逐段验证"到底能不能连上后台"

为什么需要它：
    云端部署完，K230 那边可能一声不响地失败 —— 照片全进了 SD 卡，
    网页上什么都没有，串口只看到 [up] 上传失败。到底是
        WiFi 没连 / DNS 解析不了 / 端口不通 / TLS 握手失败 /
        token 不对 / 路径写错 / nginx 413
    光看一行"上传失败"根本分不出来。这个脚本把整条链路拆成 7 段，
    每段单独判定，卡在哪一步、下一步该改什么，全都直接打出来。

用法：
    1. 先在 k230_trigger.py 里把 SERVER_URL / API_TOKEN 填好（本脚本直接读它）
    2. CanMV IDE 打开本文件运行（不用开相机，也不占 KPU）
    3. 看最后那段汇总

也可以在电脑上跑（用 CPython 验证语法和逻辑）：
    python tools/cloud_check.py https://catdog.example.com 你的token
"""

import sys
import time

# ---- 时间兼容层（PC 上跑的时候没有 ticks_ms）----
try:
    _ticks = time.ticks_ms
    _diff = time.ticks_diff
except AttributeError:
    def _ticks():
        return int(time.time() * 1000)

    def _diff(a, b):
        return a - b


# ============================================================
# 测试用图：一张 16x16 的合法 JPEG
# ============================================================
# 不用相机、不用 image 模块自己编码 —— 硬编码一张最小的合法 JPEG，
# 这样连"图片编码错了"这个变量都排除掉。
# 服务端会用 Pillow 解码算缩略图，所以必须是真 JPEG，不能是随便几个字节。
_TEST_JPEG_HEX = (
    "ffd8ffe000104a46494600010100000100010000ffdb0043002016181c1814201c1a1c24222026305034302c"
    "2c3062464a3a5074667a787266706e8090b89c8088ae8a6e70a0daa2aebec4ced0ce7c9ae2f2e0c8f0b8cace"
    "c6ffdb004301222424302a305e34345ec6847084c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6"
    "c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6ffc000110800100010030122000211010311"
    "01ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303"
    "020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f024"
    "33627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a63646566"
    "6768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7"
    "b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc400"
    "1f0100030101010101010101010000000000000102030405060708090a0bffc400b511000201020404030407"
    "05040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a"
    "162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a63646566676869"
    "6a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9"
    "bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c030100"
    "02110311003f00bd451454167fffd9"
)


def test_jpeg():
    """把十六进制串还原成 bytes。MicroPython 没有 binascii.unhexlify 的话手动解。"""
    try:
        import binascii
        return binascii.unhexlify(_TEST_JPEG_HEX)
    except Exception:
        pass
    # 手写兜底：MicroPython 各固件的 binascii 支持不一致
    n = len(_TEST_JPEG_HEX) // 2
    out = bytearray(n)
    h = _TEST_JPEG_HEX
    for i in range(n):
        out[i] = int(h[i * 2:i * 2 + 2], 16)
    return bytes(out)


# ============================================================
# 读配置：优先从主程序读，保证测的就是实际在用的值
# ============================================================
def load_config(argv):
    """返回 (server_url, api_token, timeout_s)。

    为什么直接读 k230_trigger.py 而不是在这里再写一份：
        自检脚本和主程序各写一套配置，是排查时最常见的自欺欺人 ——
        测的时候通，跑起来不通，因为两边填的不一样。
        k230_trigger.py 的模块顶层只有常量（libs 都在函数里 import），
        所以 import 它不会启动相机、也不会加载模型。
    """
    if len(argv) >= 2:
        # 命令行给的优先（电脑上验证，或者临时测一个别的地址）
        url = argv[1]
        tok = argv[2] if len(argv) >= 3 else ""
        return url, tok, 8

    try:
        import k230_trigger as C
        return (getattr(C, "SERVER_URL", ""),
                getattr(C, "API_TOKEN", ""),
                getattr(C, "UPLOAD_TIMEOUT_S", 8))
    except Exception as e:
        print("[cfg] 读不到 k230_trigger.py（%s），改用空配置" % e)
        return "", "", 8


def parse_url(s):
    """SERVER_URL -> (scheme, host, port, base_path)。和主程序 parse_server_url 同一套规则。"""
    s = (s or "").strip()
    if "://" not in s:
        s = "http://" + s
    scheme = s[:s.find("://")].lower()
    rest = s[s.find("://") + 3:]
    path = ""
    j = rest.find("/")
    if j >= 0:
        rest, path = rest[:j], rest[j:]
    port = 443 if scheme == "https" else 80
    if ":" in rest:
        h, p = rest.rsplit(":", 1)
        try:
            port = int(p)
            rest = h
        except Exception:
            pass
    if path.endswith("/"):
        path = path[:-1]
    return scheme, rest, port, path


# ============================================================
# HTTP 客户端（够用就行，不引第三方）
# ============================================================
def _sock_send(s, data):
    """发数据。MicroPython 的 socket 是流式的（write），CPython 是 send/sendall。"""
    if hasattr(s, "write") and not hasattr(s, "sendall"):
        return s.write(data)
    if hasattr(s, "sendall"):
        return s.sendall(data)
    return s.write(data)


def _sock_recv(s, n):
    """读数据。MicroPython 用 read，CPython 用 recv。"""
    if hasattr(s, "read") and not hasattr(s, "recv"):
        return s.read(n)
    if hasattr(s, "recv"):
        return s.recv(n)
    return s.read(n)


def _read_head(sock):
    """读到 \\r\\n\\r\\n 为止，返回 (status_code, headers_text)。"""
    buf = b""
    while True:
        try:
            c = _sock_recv(sock, 1)
        except Exception:
            c = None
        if not c:
            break
        buf += c
        if buf.endswith(b"\r\n\r\n"):
            break
        if len(buf) > 4096:      # 防止服务端不回换行把内存吃光
            break
    first = buf.split(b"\r\n", 1)[0].decode() if buf else ""
    code = 0
    parts = first.split(" ")
    if len(parts) >= 2:
        try:
            code = int(parts[1])
        except Exception:
            code = 0
    return code, buf.decode() if buf else ""


def http_call(scheme, host, port, method, path, body, token, timeout_s,
              ssl_verify=False):
    """发一个请求，返回 (status_code, head_text)。抛异常表示连接层就失败了。

    status 0 表示连上了但没读到状态行（对端不是 HTTP，或者直接关了连接）。
    """
    import socket

    s = socket.socket()
    try:
        # settimeout 在 K230 上可能不支持，失败就退回阻塞 + 无超时
        try:
            s.settimeout(timeout_s)
        except Exception:
            pass
        s.connect((host, port))

        if scheme == "https":
            s = _wrap_tls(s, host, ssl_verify)

        if token:
            sep = "&" if "?" in path else "?"
            path = "%s%stoken=%s" % (path, sep, token)

        hosthdr = host if port in (80, 443) else "%s:%d" % (host, port)
        req = "%s %s HTTP/1.1\r\nHost: %s\r\n" % (method, path, hosthdr)
        req += "Connection: close\r\n"
        if body:
            req += "Content-Type: image/jpeg\r\n"
            req += "Content-Length: %d\r\n" % len(body)
        req += "\r\n"

        # \r\n 结尾一定要保留：有的服务端对 CRLF 敏感
        _sock_send(s, req.encode())
        if body:
            _sock_send(s, body)
        return _read_head(s)
    finally:
        try:
            s.close()
        except Exception:
            pass


def _wrap_tls(sock, host, verify):
    """套 TLS。和主程序同一套降级策略：SSLContext -> wrap_socket -> 没有 ssl。"""
    try:
        import ssl
    except Exception:
        raise OSError("固件没有 ssl 模块，https 用不了（只能 http）")

    if hasattr(ssl, "SSLContext"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if not verify:
            try:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            except Exception:
                pass
        return ctx.wrap_socket(sock, server_hostname=host)

    # 老固件
    try:
        return ssl.wrap_socket(sock, server_hostname=host)
    except TypeError:
        return ssl.wrap_socket(sock)


# ============================================================
# 结论翻译：状态码 -> 人话
# ============================================================
def explain(code, token_set):
    """把这个状态码最可能的原因说清楚。排查时最花时间的就是这一步。"""
    if code == 200:
        return "正常"
    if code == 401:
        return "服务端要鉴权但没收到 token —— 检查 API_TOKEN"
    if code == 403:
        return ("token 不匹配。K230 的 API_TOKEN 和服务端 CATDOG_TOKEN "
                "必须一字不差" if token_set else
                "服务端开了鉴权，但 K230 的 API_TOKEN 是空的")
    if code == 404:
        return ("路径不对。检查 SERVER_URL 有没有多写/少写子路径，"
                "以及 nginx 的 location 有没有转发")
    if code == 405:
        return "方法不对（应该是 POST /api/v1/visit、GET /api/v1/health）"
    if code == 413:
        return "照片太大被拒。nginx 的 client_max_body_size 没改（默认 1M）"
    if code == 502 or code == 504:
        return ("nginx 连不上后面的服务 —— 服务没启动，或者 CATDOG_HOST "
                "绑的不是 127.0.0.1")
    if code == 0:
        return ("连上了但对方没说 HTTP —— 端口上跑的不是这个服务，"
                "或者中间有东西在拦")
    return "没见过的状态码，看上面返回的原文"


def main():
    argv = sys.argv if hasattr(sys, "argv") else [""]
    server_url, token, timeout_s = load_config(argv)

    print("=" * 60)
    print("K230 -> 云端 连通性自检")
    print("=" * 60)

    # ---------- 0. 配置 ----------
    print("[0/7] 配置")
    print("      SERVER_URL   = %r" % (server_url,))
    print("      API_TOKEN    = %s" % ("已设置(%d 位)" % len(token) if token
                                      else "空 —— 公网必须设！"))
    print("      UPLOAD_TIMEOUT_S = %s" % timeout_s)

    scheme, host, port, base = parse_url(server_url)
    if not host:
        print("      !! SERVER_URL 是空的，先去 k230_trigger.py 填")
        return
    print("      解析结果: scheme=%s host=%s port=%d base=%r"
          % (scheme, host, port, base))

    ok_count = 0
    fail = []

    # ---------- 1. WiFi ----------
    print("")
    print("[1/7] WiFi")
    try:
        import network
        wlan = network.WLAN(network.STA_IF)
        if wlan.isconnected():
            info = wlan.ifconfig()
            print("      已连接  ip=%s  mask=%s  gw=%s" % (info[0], info[1], info[2]))
            ok_count += 1
        else:
            print("      !! 没连上。SSID/PASS 对不对？2.4G 和 5G 别搞混")
            fail.append("WiFi 未连接")
    except ImportError:
        print("      （电脑上跑，跳过）")
        ok_count += 1
    except Exception as e:
        print("      !! %s" % e)
        fail.append("WiFi 异常: %s" % e)

    # ---------- 2. ssl 模块 ----------
    print("")
    print("[2/7] TLS 支持")
    has_ssl = True
    try:
        import ssl          # noqa: F401
        print("      固件有 ssl 模块")
        ok_count += 1
    except Exception:
        has_ssl = False
        print("      固件**没有** ssl 模块")
        if scheme == "https":
            print("      !! SERVER_URL 是 https，但没 ssl —— 这个组合一定连不上")
            print("         要么换 http（公网不推荐），要么烧带 ssl 的固件")
            fail.append("https 需要 ssl 模块，但固件没有")
        else:
            print("      （现在是 http，不影响）")
            ok_count += 1

    # ---------- 3. DNS ----------
    print("")
    print("[3/7] DNS 解析 %s" % host)
    ip = None
    try:
        import socket
        try:
            ip = socket.getaddrinfo(host, port)[0][-1][0]
        except AttributeError:
            ip = socket.getaddrinfo(host, port)[0][4][0]
        print("      -> %s" % ip)
        ok_count += 1
    except Exception as e:
        print("      !! 解析失败: %s" % e)
        print("         能上网但解析不了：DNS 服务器不可用，或者域名拼错")
        fail.append("DNS 解析失败")
        # 解析不了，后面 4~6 步都没意义了
        print("")
        print("=" * 60)
        print("停在 DNS，后面的测试跳过。")
        print("=" * 60)
        return

    # ---------- 4. TCP ----------
    print("")
    print("[4/7] TCP 连接 %s:%d" % (host, port))
    t0 = _ticks()
    try:
        import socket
        c = socket.socket()
        try:
            c.settimeout(timeout_s)
        except Exception:
            pass
        c.connect((host, port))
        c.close()
        print("      通  (%d ms)" % _diff(_ticks(), t0))
        ok_count += 1
    except Exception as e:
        print("      !! 连不上: %s  (%d ms)" % (e, _diff(_ticks(), t0)))
        print("         最常见的原因，按可能性排：")
        print("         1) 云服务器安全组没放开 %d 端口" % port)
        print("         2) 服务没启动（systemctl status catdog）")
        print("         3) 服务绑的是 127.0.0.1 而你在直连公网 IP")
        print("         4) 家里网络挡了出站（少见，公司网才会）")
        fail.append("TCP %d 不通" % port)
        print("")
        print("=" * 60)
        print("停在 TCP，后面的 HTTP 测试跳过。")
        print("=" * 60)
        return

    # ---------- 5. TLS 握手 ----------
    print("")
    if scheme == "https":
        print("[5/7] TLS 握手")
        if not has_ssl:
            print("      !! 跳过（没有 ssl 模块）")
            fail.append("不能做 TLS")
        else:
            t0 = _ticks()
            try:
                import socket
                c = socket.socket()
                try:
                    c.settimeout(timeout_s)
                except Exception:
                    pass
                c.connect((host, port))
                c = _wrap_tls(c, host, False)
                print("      握手成功  (%d ms)" % _diff(_ticks(), t0))
                c.close()
                ok_count += 1
            except Exception as e:
                print("      !! 握手失败: %s" % e)
                print("         证书域名/端口不匹配，或者对端根本不是 HTTPS")
                fail.append("TLS 握手失败")
    else:
        print("[5/7] TLS 握手 —— 跳过（http 不需要）")
        ok_count += 1

    # ---------- 6. health ----------
    print("")
    print("[6/7] GET /api/v1/health")
    health_path = (base or "") + "/api/v1/health"
    try:
        code, head = http_call(scheme, host, port, "GET", health_path,
                               None, "", timeout_s)
        if code == 200:
            print("      200 服务活着")
            ok_count += 1
        else:
            print("      !! %d  %s" % (code, explain(code, bool(token))))
            print("      返回原文: %s" % head[:300].replace("\r\n", " | "))
            fail.append("health 返回 %d" % code)
    except Exception as e:
        print("      !! 请求异常: %s" % e)
        fail.append("health 请求异常")

    # ---------- 7. 上传（关键：这才是真的那张照片走的路）----------
    print("")
    print("[7/7] POST /api/v1/visit（真的传一张图）")
    body = test_jpeg()
    print("      测试图 %d 字节" % len(body))
    visit_path = (base or "") + "/api/v1/visit?species=cat&device=selftest"

    # 7a. 不带 token：服务端开了鉴权就必须 403
    if token:
        try:
            code, head = http_call(scheme, host, port, "POST", visit_path,
                                   body, "", timeout_s)
            if code == 403 or code == 401:
                print("      不带 token -> %d  鉴权生效（正确）" % code)
                ok_count += 1
            elif code == 200:
                print("      !! 不带 token 也是 200 —— **鉴权没生效**")
                print("         公网上任何人知道地址都能往你库里塞照片")
                fail.append("服务端鉴权没生效")
            else:
                print("      不带 token -> %d  %s" % (code, explain(code, False)))
                fail.append("鉴权探测返回 %d" % code)
        except Exception as e:
            print("      !! %s" % e)
            fail.append("鉴权探测异常")
    else:
        print("      （API_TOKEN 是空的，跳过鉴权探测）")

    # 7b. 带 token：应该是 200
    try:
        code, head = http_call(scheme, host, port, "POST", visit_path,
                               body, token, timeout_s)
        if code == 200:
            print("      带 token -> 200  **上传链路通了**")
            ok_count += 1
            print("      去网页刷新看看，应该多了一条 device=selftest 的记录")
        else:
            print("      !! 带 token -> %d  %s" % (code, explain(code, bool(token))))
            print("      返回原文: %s" % head[:400].replace("\r\n", " | "))
            fail.append("上传返回 %d" % code)
    except Exception as e:
        print("      !! 上传异常: %s" % e)
        print("         超时的话把 UPLOAD_TIMEOUT_S 调大（跨省公网 8~10s 常见）")
        fail.append("上传异常")

    # ---------- 汇总 ----------
    print("")
    print("=" * 60)
    if fail:
        print("!! 还有 %d 个问题：" % len(fail))
        for f in fail:
            print("   · %s" % f)
        print("")
        print("按上面的顺序从上往下改 —— 先出现的那个才是根因，")
        print("它没好之前，后面的失败都是它引起的假象。")
    else:
        print("全部通过。K230 这边到云端是通的。")
        print("如果主程序还是不传，检查 SERVE_UPLOAD 是不是 True、")
        print("以及有没有真的检测到猫狗（没框的时候本来就不传）。")
    print("=" * 60)


if __name__ == "__main__":
    main()
