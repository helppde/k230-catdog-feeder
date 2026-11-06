# -*- coding: utf-8 -*-
"""PC 端配置"""

import os

# ---------------- 网络 ----------------
HOST = os.environ.get("CATDOG_HOST", "0.0.0.0")   # 局域网内可访问；只想本机调试就改 127.0.0.1
PORT = int(os.environ.get("CATDOG_PORT", "8000"))

# 留空 = 不校验。想防止别人乱传就填一串随机字符，
# K230 端 URL 后面加 &token=xxx（见 K230 的 config.SERVER_URL）
# ⚠️ 一旦这个服务放到公网（云服务器），**必须**填，否则任何人都能往你库里塞照片。
API_TOKEN = os.environ.get("CATDOG_TOKEN", "")

# ---------------- HTTPS（云端部署用） ----------------
# 两种做法，选一种：
#   A) 前面挂反向代理（nginx / Caddy / 云厂商 SLB），由它做 TLS —— 推荐
#      这时这里什么都不用改，反代把 https 转成 http 打给本服务
#   B) 本服务自己开 TLS（没有反代时用）
#      set CATDOG_TLS_CERT=C:\certs\fullchain.pem
#      set CATDOG_TLS_KEY=C:\certs\privkey.pem
#      set CATDOG_PORT=443
TLS_CERT = os.environ.get("CATDOG_TLS_CERT", "")
TLS_KEY = os.environ.get("CATDOG_TLS_KEY", "")

# ---------------- 数据 ----------------
PC_DIR = os.path.dirname(os.path.abspath(__file__))
# 环境变量可以换数据目录，方便同一台机器跑多个实例 / 做演示不污染正式数据
DATA_DIR = os.environ.get("CATDOG_DATA_DIR", os.path.join(PC_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "visits.db")
IMG_DIR = os.path.join(DATA_DIR, "images")
WEB_DIR = os.path.join(PC_DIR, "web")

THUMB_WIDTH = 280        # 卡片缩略图宽度
JPEG_QUALITY = 86        # 重新压缩的质量（原图要是压得厉害就别再压）
MAX_UPLOAD_BYTES = 4 * 1024 * 1024

# ---------------- 个体识别 ----------------
# auto: 有 torch/torchvision 就用 ResNet18 特征，没有就退回手工特征
EMBED_BACKEND = "auto"

# 余弦相似度阈值：
#   高于它 -> 认成"老熟人"；低于它 -> 新建一个个体
# 这个值一定要按你自己的画面调：先把 MATCH_THRESHOLD 调到 0.99（几乎不合并）跑几天，
# 看网页上每次到访的 sim 值分布，再把阈值卡在"同一个人"和"不同人"之间。
# ResNet18 特征：同一个体常见 0.85~0.95，不同个体常见 0.45~0.75。
MATCH_THRESHOLD = 0.82

# 每个个体最多存多少个特征向量（越多越不容易漏认，但也越容易误合并）
MAX_EMB_PER_INDIVIDUAL = 12

# 自动识别做不到 100%：认错了在网页上点"合并"或改个名字即可，
# 统计数字按 individual_id 走，人工纠正会立即生效。

SPECIES_NAMES = {"cat": "猫", "dog": "狗", "unknown": "未知"}

# 时段划分（左闭右开），用于"什么时间段来的"
PERIODS = [
    ("深夜", 0, 5),
    ("清晨", 5, 8),
    ("上午", 8, 11),
    ("中午", 11, 13),
    ("下午", 13, 17),
    ("傍晚", 17, 20),
    ("夜晚", 20, 24),
]
