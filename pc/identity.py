# -*- coding: utf-8 -*-
"""
"这只猫/这只狗是第几次来" —— 个体区分

原理很朴素：
    把照片裁成动物本体 -> 变成一个向量（特征）-> 和库里已有个体比余弦距离
    够像  -> 这就是老朋友，个体 A
    不够像 -> 新面孔，建一个个体 B

两套特征后端（自动选，`config.EMBED_BACKEND = "auto"`）：

  torch    ResNet18（ImageNet 预训练，去掉最后的分类层，取 avgpool 的 512 维）
           优点：对毛色/斑纹/体型都有一定感知，同一个体常见 0.85~0.95
           缺点：要装 torch + torchvision；CPU 上一张约 30~60ms

  numpy    手工特征：4×4 网格 × 每格 8 个色相直方图(按饱和度加权) + 亮度均值/方差
           优点：零依赖、极快
           缺点：只认颜色分布，对"两只同色猫"几乎无法区分

⚠️ 老老实实说：单摄像头 + 一张图，个体识别不可能 100%。
   - 同一个体不同姿态/光照/距离，相似度会掉；
   - 两只同色同体型的猫，相似度可能反而很高。
   所以网页上提供"合并"和"改名"，人工纠正会立刻反映到统计里。
   要追求高准确率，只能多拍角度、用专门的动物 ReID 模型训练，
   或者给每只猫戴不同颜色的项圈（最省事、最可靠）。
"""

import threading

import numpy as np

import config as PC

_LOCK = threading.Lock()


# ------------------------------------------------ 相似度
def l2norm(vec):
    a = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(a))
    if n <= 1e-8:
        return a
    return a / n


def cosine(a, b):
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    if a.shape != b.shape:
        return -1.0
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 1e-8 or nb <= 1e-8:
        return -1.0
    return float(np.dot(a, b) / (na * nb))


class Embedder(object):
    """把一张动物照片变成一个定长向量。"""

    def __init__(self):
        self.backend = None
        self.dim = 0
        self._model = None
        self._pre = None
        self._torch = None
        self._device = "cpu"
        self._ready = threading.Event()

    # ------------------------------------------------ 初始化
    def load(self):
        if self.backend is not None:
            return self.backend
        with _LOCK:
            if self.backend is not None:
                return self.backend
            mode = PC.EMBED_BACKEND
            if mode in ("auto", "torch") and self._load_torch():
                self.backend = "torch"
            elif mode == "torch":
                print("[identity] 指定了 torch 但不可用，退回手工特征")
                self._load_handcrafted()
                self.backend = "handcrafted"
            else:
                self._load_handcrafted()
                self.backend = "handcrafted"
            print("[identity] 特征后端 = %s (%d 维)"
                  % (self.backend, self.dim))
            self._ready.set()
            return self.backend

    def _load_torch(self):
        try:
            import torch
            import torchvision
            from torchvision import transforms
        except Exception as e:
            print("[identity] torch/torchvision 不可用: %s" % e)
            return False
        try:
            weights = torchvision.models.ResNet18_Weights.IMAGENET1K_V1
            model = torchvision.models.resnet18(weights=weights)
        except Exception:
            model = torchvision.models.resnet18(pretrained=True)
        model.fc = torch.nn.Identity()          # 只要特征，不要分类
        model.eval()
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(self._device)
        self._model = model
        self._torch = torch
        self._pre = transforms.Compose([
            transforms.Resize((256, 256)),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225]),
        ])
        self.dim = 512
        return True

    def _load_handcrafted(self):
        self.dim = 4 * 4 * (8 + 2)   # 4x4 网格 × (8 色相 + 亮/暗)

    def warmup(self):
        """启动后用后台线程把模型加载好，别让第一张照片等 5 秒。"""
        def _w():
            try:
                self.load()
                from PIL import Image
                self.embed(Image.new("RGB", (224, 224), (128, 128, 128)))
            except Exception as e:
                print("[identity] 预热失败: %s" % e)
        t = threading.Thread(target=_w, name="embed-warmup")
        t.daemon = True
        t.start()

    # ------------------------------------------------ 推理
    def embed(self, pil_img):
        if self.backend is None:
            self.load()
        if pil_img is None:
            return None
        try:
            if self.backend == "torch":
                return self._embed_torch(pil_img)
            return self._embed_handcrafted(pil_img)
        except Exception as e:
            print("[identity] 特征提取失败: %s" % e)
            return None

    def _embed_torch(self, pil_img):
        img = pil_img.convert("RGB")
        t = self._pre(img).unsqueeze(0).to(self._device)
        with self._torch.no_grad():
            v = self._model(t).squeeze(0).float().cpu().numpy()
        return l2norm(v).tolist()

    def _embed_handcrafted(self, pil_img):
        img = pil_img.convert("HSV").resize((64, 64))
        a = np.asarray(img, dtype=np.float32)
        h, s, v = a[..., 0], a[..., 1], a[..., 2]
        out = []
        for gy in range(4):
            for gx in range(4):
                sl = (slice(gy * 16, (gy + 1) * 16), slice(gx * 16, (gx + 1) * 16))
                hs = h[sl].ravel()
                ws = s[sl].ravel()
                hist, _ = np.histogram(hs, bins=8, range=(0, 256), weights=ws)
                out.extend((hist / (ws.sum() + 1e-6)).tolist())
                vs = v[sl].ravel()
                out.append(float(vs.mean()) / 255.0)
                out.append(float(vs.std()) / 255.0)
        return l2norm(out).tolist()


# 全局单例（模型只加载一次）
EMBEDDER = Embedder()


# ------------------------------------------------ 匹配
def best_match(db, species, vec):
    """在 species 这个物种的所有个体里找最像的。

    返回 (individual_id 或 None, 相似度)
    """
    if vec is None:
        return None, -1.0
    best_id = None
    best_sim = -1.0
    for iid, other in db.embeddings_of_species(species):
        sim = cosine(vec, other)
        if sim > best_sim:
            best_sim = sim
            best_id = iid
    if best_sim < PC.MATCH_THRESHOLD:
        return None, best_sim
    return best_id, best_sim
