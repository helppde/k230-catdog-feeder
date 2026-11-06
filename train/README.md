# 模型：训练猫狗检测 + 转 K230 kmodel

> **如果你现在用的是 COCO 预训练模型（cat=15 / dog=16），本文可以先跳过。**
> `config.py` 里已经配好了 80 类 COCO 的名字和 `KEEP_CLASSES=(15,16)`，
> 拷一个 80 类的 kmodel 到板子上就能跑。
> 等你发现 COCO 模型在夜间/远处漏检太多，再回来按本文自己训。

`k230_trigger.py` 需要一个 YOLO kmodel，下面是从零到上板的流程。
如果自己训两类模型，记得把 `config.py` 改成：

```python
LABELS = ["cat", "dog"]
CLASS_CAT = 0
CLASS_DOG = 1
KEEP_CLASSES = (0, 1)
```

---

## 1. 数据集

### 方案 A：自己拍（推荐）

流浪猫狗场景和 COCO 差别很大（夜间、仰拍、门框、遮挡），自己拍效果明显更好。

- 用 K230 本身或手机在**实际安装位置**拍，白天/傍晚/夜间各来一批；
- 500~1500 张就够训一个能用的模型；
- 用 [labelImg](https://github.com/HumanSignal/labelImg) / [X-AnyLabeling](https://github.com/CVHub520/X-AnyLabeling) 标 YOLO 格式；
- 负样本很重要：把"空画面 / 只有人 / 只有塑料袋"的图也放进去，`images` 里不建对应 txt 即可。

目录结构：

```
dataset/
  images/train/*.jpg
  images/val/*.jpg
  labels/train/*.txt
  labels/val/*.txt
data.yaml
```

`data.yaml`：

```yaml
path: ./dataset
train: images/train
val: images/val
names:
  0: cat
  1: dog        # 顺序必须和 config.py 的 CLASS_CAT / CLASS_DOG 一致
```

### 方案 B：拿公开数据凑

- COCO 里 `cat` 的类别号是 15，`dog` 是 16，抽这两个类出来重标成 0/1；
- 或用 Roboflow 上现成的 stray cat/dog 数据集（注意许可证）。

---

## 2. 训练

```bash
pip install ultralytics

yolo detect train \
  model=yolo11n.pt \
  data=data.yaml \
  imgsz=320 \
  epochs=100 \
  batch=32 \
  patience=30 \
  name=catdog320
```

**为什么用 320**：K230 上 640 输入基本跑不动实时；320 在 K230 KPU 上大概
能到 15~30 FPS，对"有没有猫狗"这个任务足够了。

---

## 3. 导出 ONNX

K230 的 nncase 对算子有限制，注意：

- **opset 用 11 或 12**（更高版本容易有不支持的算子）；
- 固定输入尺寸，**不要 dynamic**；
- 输出不要带 NMS（后处理放在 Python 里，`libs/YOLO.py` 已经做了）。

```bash
yolo export \
  model=runs/detect/catdog320/weights/best.pt \
  format=onnx \
  imgsz=320 \
  opset=12 \
  simplify=True \
  dynamic=False
```

自己验一下输出形状，应该是 `1x(4+nc)x8400` 这类（不同 ultralytics 版本略有差异），
用 Netron 打开确认没有 `NonMaxSuppression` 节点。

---

## 4. 转 kmodel（nncase）

推荐直接用嘉楠官方的 nncase docker 镜像，避免本地依赖地狱：

```bash
docker run --rm -v $PWD:/work -w /work \
  ghcr.io/kendryte/nncase:latest \
  nncase compile best.onnx cat_dog_yolo11n_320.kmodel \
    --target k230 \
    --dataset ./calib \
    --input-type uint8 \
    --input-layout NCHW \
    --input-shape 1,3,320,320 \
    --output-type float32 \
    --quant-type uint8
```

- `--dataset ./calib`：放 20~50 张实拍图做量化校准，**必须是你实际场景的图**
  （夜间、逆光都要有），否则量化后掉点很严重；
- `-t k230 --quant-type uint8`：K230 KPU 用 uint8 量化推理最快；
- 转完大概几 MB，拷到板子 `/sdcard/kmodel/`（出厂卡的 `kmodel` 目录就在这，
  厂商自带的模型也都在里面）。

> 如果 nncase 报不支持的算子，先试 `opset=11` + `simplify=False` 重新导出；
> 还不行就把 ultralytics 降到 `8.1.x` 再导（不同版本的 ONNX 图差异挺大）。

---

## 5. 板上验证

单独跑一个最小检测脚本（或直接 `k230_trigger.py` 把 `SIMULATE_DETECTION=False`）：

```python
import config as C
from detector import build_detector

det = build_detector()      # 会打印用了 libs.YOLO 的哪个类
print(det.detect(frame))    # 期望 [x, y, w, h, 0|1, conf]
```

如果 `build_detector()` 打印"回退到模拟检测"，说明固件里的 `libs/YOLO.py`
类名和预期不一致，直接改 `detector.py` 的 `_load_yolo_cls()` 里的 `names` 元组。

---

## 6. 效果不达标时

| 现象 | 先试 |
|---|---|
| 夜里完全检不到 | 加红外补光/白光灯，把夜间图放进训练集 |
| 把猫认成狗 | 提高 `CONF_THRESH`，补几张易混样本重训 |
| 远处小目标检不到 | 训练时提高 `imgsz`，或只让触发区靠近相机 |
| 帧率太低 | 换 `yolo11n` / 降输入到 256，或减小 `RGB888P_SIZE` |
| 误检塑料袋/鞋子 | 加负样本，这是最有效的办法 |
