# K230 流浪猫狗识别 · 自动放饭系统

**K230 只做视觉**（识别猫/狗、拍照上传、决定"该开饭了"）
**执行板做执行**（42 步进 + 2 个舵机 + 盆口超声波测粮）
**PC 做统计**（网页上看每只猫/狗来了几次、都什么时间段来的）

三块之间只有两条链路：

```
K230 --GPIO 电平（有动物=高）--> 执行板 D2      "有动物"信号，单向
K230 --WiFi/HTTP-JPEG-------> PC            有动物期间每 60 秒传一张照片
```

> **不用串口。** K230 把一根 GPIO 的电平实时反映"现在有没有猫/狗在盆边"：
> 检测到就**一直保持高电平**，连续 2 秒没检测到才拉低。
> 执行板看到上升沿就开始跑一整个循环（用超声波测盆里有没有粮 → 决定要不要先出粮
> → 开门 → 推盆 → 保持 60s → 退回 → 关门）。
> 整轮全部由执行板本地完成，不需要 K230 再发指令、也不等回执。

---

## 1. 系统架构

```
┌─────────────── K230（视觉 + 上传）───────────────┐
│ 摄像头 → YOLO kmodel → 猫/狗检测框               │
│        ↓                     ↓                   │
│   触发区判定            裁剪动物 → JPEG          │
│        ↓                     ↓                   │
│  存在消抖 → GPIO42 电平    每 60s POST 到 PC      │
└───────┬───────────────────────────┬──────────────┘
        │ 单根 GPIO（3.3V 电平）      │ WiFi HTTP
        ↓                           ↓
┌───────┴─────────────┐   ┌─────────┴──────────────────┐
│ 执行板 Nano         │   │ PC 服务端（python 标准库） │
│  超声测距 → 分支    │   │  sqlite 存照片/到访/特征    │
│  舵机1 门           │   │  ResNet18 特征 → 认出个体   │
│  舵机2 粮仓         │   │  网页：次数 / 时段 / 合并   │
│  42 步进 + L298N    │   └────────────────────────────┘
│  60s 保持 + 到位断电 │
└─────────────────────┘
```

### 信号为什么是"持续电平"而不是"脉冲"

高电平 = 有动物，低电平 = 没有。执行板靠**上升沿**触发，触发后立刻
`armed = false`，只有等信号回到低电平才重新武装。于是：

| 场景 | 电平 | 执行板 |
|---|---|---|
| 猫来了 | 低→高（一个上升沿） | 跑一轮 |
| 猫蹲着吃 10 分钟 | 一直高 | **每 2 分钟补放一轮**（执行板自己判断） |
| 猫走了 | 高→低 | 停止补放，等待下次上升沿 |
| 猫又回来 | 低→高 | 跑一轮 |

> 补放节奏完全由执行板决定（`RE_FEED_WHILE_HIGH_MS`，默认 120000 = 2 分钟）。
> K230 只负责如实反映"有动物/没动物"，不参与"要不要补粮"的判断。
> 所以 K230 死机、WiFi 断了，只要电平还高着，执行板照样按周期补粮。

执行板靠**上升沿**判断"动物刚来"，靠**持续高电平 + 2 分钟周期**判断"动物还在吃，该补粮"，
靠**电平回落**判断"动物走了"。K230 这边因此完全不用管补粮节奏 ——
它只需要把电平如实反映出来，剩下的判断都在执行板本地做。
代价是电平必须稳：单帧检测会闪，所以 K230 那边有消抖（见 5.2），
否则会冒出假上升沿、多放一次饭。

### 为什么把"一整轮"交给执行板，而不是 K230 一步步发

1. **粮食检测在执行板上**。判断"盆里有没有粮"必须板子自己测（超声波）。
2. **一轮 74 秒**。如果分成「开门→前进→保持→退回→关门」五条命令，
   任何一条在 60 秒保持期间被漏掉，机构就停在半开状态。
3. **60 秒保持期间 K230 完全不用管**。K230 只维护一个电平，画面/识别/上传全部照常。
4. **少一根线就少一处故障**。GPIO 单线没有波特率/交叉/电平转换这些问题，
   也不用担心串口掉包卡住机构。

### 这个方案放弃了什么（已知取舍）

| 没有的东西 | 影响 | 现在的兜底 |
|---|---|---|
| 回执 ACK | K230 不知道执行板到底动了没有 | `armed` 边沿锁：同一段高电平只跑一轮 |
| 急停 | K230 没法中途叫停 | 执行板动作自洽，一轮跑完自己归位 |
| 参数下发 | 出粮时长/角度写死在固件里 | 改 `.ino` 重新烧，或用 `PIN_BUSY` 回一根线 |
| 状态回读 | K230 看不到"盆里有没有粮" | 不需要——判断在板子上本地做 |

想补回"执行板在忙"这个信息，可以接 `PIN_BUSY`（默认 `-1` 表示没接）：
执行板跑一轮期间拉高，K230 等它变低再允许下次触发。注意 Arduino 输出是 5V，
回接 K230 必须分压到 3.3V。

---

## 2. 硬件与接线

| 部件 | 说明 |
|---|---|
| K230 | 亚博智能 K230，CanMV MicroPython，接摄像头 + 一根触发线 + WiFi |
| 执行板 | **Arduino Nano**（ATmega328P, 5V, 16MHz）—— `arduino/simple_trigger/simple_trigger.ino` |
| 42 步进 | L298N 驱动，A+/A-/B+/B- 接 OUT1~OUT4，带丝杠滑台 |
| 舵机 ×2 | 舵机1 = 门（90°关 / 0°开），舵机2 = 粮仓（90°关 / 0°开） |
| HC-SR04 超声波 | 装在盆口上方朝下照，用距离判断盆里有没有粮 |
| 电源 | 舵机 5V/3A 独立；步进单独一路；**全部共地** |

### 2.1 K230 ↔ 执行板（一根触发线，不用串口）

> 引脚已按**亚博 YAHBOOM K230 的实际引出**核对过（官方文档「2.基础例程 / 1.FPIOA引脚分配」）。
> 这个模块只引出 7 个 GPIO，全在散热片旁 12pin 排针上：
>
> | 排针脚 | GPIO | 可复用功能 | 能不能当触发脚 |
> |---|---|---|---|
> | 1 | GPIO42 | GPIO42 / UART1_RTS / PWM0 / QSPI1_D2 | ✅ **默认普通 GPIO，用它** |
> | 2 | GPIO43 | GPIO43 / UART1_CTS / PWM1 / QSPI1_D3 | ✅ 次选 |
> | 8 | GPIO34 | IIC1_SCL / IIS_D_IN0 / PDM_IN3 | ⚠️ 用音频就没了 |
> | 10 | GPIO35 | IIC1_SDA / IIS_D_OUT0 / PDM_IN1 | ⚠️ 同上 |
> | 3 | GPIO33 | IIC0_SDA / IIS_WS / UART3_RXD | ⚠️ 同上 |
> | 5 | GPIO32 | IIC0_SCL / IIS_CLK / UART3_TXD | ⚠️ 同上 |
> | 6 | GPIO26 | **MMC1_CLK** / PDM_CLK | ❌ **SDIO 时钟，用了 WiFi 就别碰** |

```
K230 排针第 1 脚 (GPIO42, 3.3V) ──────→ 板 D2 (触发输入)   ← 直接接
K230 排针第 4 脚 (GND)        ──────→ 板 GND              ← 必须共地
```

1. **只接两根线**：信号 + 地。K230 输出 3.3V，Arduino 读 3.3V 高电平没问题，不用电平转换。
2. **必须共地**，哪怕两边各自供电。
3. **别接 5V 到 GPIO42**。排针第 9/11 脚是 5V 输出，接错就烧 K230。
4. **GPIO26 留给 WiFi**。板载 SDIO WiFi 占用 MMC1，动它 WiFi 直接不通，也就没法上传照片了。
5. 触发是**单向电平**：K230 检测到动物就一直保持高电平，动物走了才拉低。
   执行板靠上升沿放第一轮；如果电平一直高，它自己每 2 分钟补放一轮。
   想加一根"执行板在忙"的反馈线，见下面 2.1b。

#### 2.1b 可选：`PIN_BUSY` 反馈线（不接也能跑）

执行板跑一轮期间把某个脚拉高，K230 可以把这当作"它还在忙"。

```
板 D6 (5V 输出) ──[1k]──┬──→ K230 排针第 2 脚 (GPIO43)
                        │
                      [2k]
                        │
                       GND
```

固件里把 `PIN_BUSY` 从 `-1` 改成 `6`，`k230_trigger.py` 里 `BUSY_PIN = 43`。
**Arduino 输出 5V，接 K230 必须 1k/2k 分压到 3.3V。**

接上之后 K230 能打出"执行板开始跑 / 跑完一轮"的日志，联调时很有用
（否则 K230 完全不知道板子动没动）。
> 不接也完全没问题 —— 信号语义不依赖它，`armed` 边沿锁已经保证了"同一段高电平只放一次"。

### 2.2 执行板引脚

| 功能 | 引脚 | 说明 |
|---|---|---|
| 舵机1（门） | D3 | 信号线；电源单独供，GND 与板子共地 |
| 舵机2（粮仓） | D5 | |
| L298N IN1 | D7 | 线圈 A |
| L298N IN2 | D8 | |
| L298N IN3 | D9 | 线圈 B（Servo 库占了 Timer1，D9/D10 的 analogWrite 失效，但 digitalWrite 正常，步进只用开关量） |
| L298N IN4 | D10 | |
| L298N ENA | — | **不接**（填 `-1`）。L298N 上的 ENA/ENB 跳线帽要留着，桥才使能 |
| L298N ENB | — | 同上 |
| HC-SR04 TRIG | D11 | 输出，发 10µs 高脉冲 |
| HC-SR04 ECHO | D12 | 输入，5V 电平直接读，不用分压 |
| 行程限位（可选） | A0/A1 | 装了改 `PIN_LIMIT_OUT/IN`，推到底自动停，最安全 |
| 触发输入 | D2 | ← K230 GPIO42，3.3V 电平（有动物=高）。**只接信号 + GND 两根线** |
| 串口 | D0/D1 | **空着**（本方案不用串口）→ 可以插 USB 边跑边看调试输出 |

L298N 接线：

```
42 步进 A+ → OUT1     A- → OUT2     B+ → OUT3     B- → OUT4
步进电源 +  → L298N +12V
步进电源 -  → L298N GND 和 Nano GND（必须连一起）
ENA/ENB 跳线帽 **留着别拔**（不接 ENA/ENB，靠跳线帽把桥硬使能）
```

### 2.2.1 板子是 Arduino Nano，这几处要专门注意

固件已按 **ATmega328P @16MHz / 5V** 实编译验证过（不是"应该能编"）：

```
Program:   10036 bytes (30.6% Full)     ← flash 30.7KB 够用
Data:        737 bytes (36.0% Full)     ← 静态 RAM 用掉 737B
```

> Nano 只有 **2KB SRAM**。737B 是静态部分（Serial 双缓冲 128B + Servo 表 + 全局变量），
> 剩约 1.3KB 给栈 —— 现在够，但**别再往全局加 `String` 或大数组**，
> 超过 RAM 的表现是"跑一会儿莫名重启/舵机乱动"，很难查。

| Nano 上的坑 | 说明 |
|---|---|
| **上传速度** | 老版 Nano（旧 bootloader）要在 IDE 里选 `处理器 → ATmega328P (Old Bootloader)`，否则报 `avrdude: stk500_recv(): programmer is not responding` / `not in sync: resp=0x00`。新版选普通 `ATmega328P`。**这块 CH340 板实测就是旧 bootloader（57600，FW 1.16）** —— 判断方法见第 11 节第一条 |
| **别用 VIN 喂 12V** | 板上 NCP1117 线性降压，12V 进、5V 出，光板子就发烫；再带两个舵机必过热保护。**USB 供电或 5V 脚直接喂 5V** |
| **D11 / D12 是 MOSI / MISO** | 就是 ISP 排针那两根。本项目不用 SPI，可以放心用；但**以后想加 SD 卡 / nRF24 / SPI 屏幕就冲突了**，得换引脚 |
| **D9 / D10 没有 PWM** | `Servo` 库在 328P 上独占 Timer1，`analogWrite(9/10)` 失效。步进只用 `digitalWrite` 开关量，所以没事；但**别再往这两个脚加需要调速的东西** |
| **A6 / A7 只能当模拟口** | 没有数字功能。装行程限位只能接 A0~A5（= D14~D19） |
| **D13 带板载 LED + 电阻** | 不要拿它当输入（会被 LED 电路拉偏） |
| **3.3V 脚只有几十 mA** | 是 USB 芯片（FT232RL / CH340）给的，标称 50mA 左右，别拿来给传感器供电 |
| **ECHO 是 5V 输出** | 进 Nano 的 5V 输入正好匹配，**不用分压**。（如果哪天换成 3.3V 的板子，必须加分压） |

### 2.3 ⚠️ L298N 带 42 步进的三个坑（务必看）

**坑 1：L298N 没有限流。**
42 步进的相电阻通常 1.5~3Ω，12V 直接上，电流 = (12-2)/2Ω ≈ 5A，而 L298N 标称 2A/路、
实际不加散热片 1A 就开始烫。轻则热保护抖动丢步，重则烧桥。

- 最省事的缓解：**步进电源降到 6~9V**（力矩会小一点，但丝杠滑台够用）；
- 最正确的做法：**换 A4988 / TMC2208 / DRV8825**（几块钱，带限流和细分，
  串口/跳线就能调电流，也不用 ENA/ENB 两路 PWM）。本固件的命令协议不用改，
  只要把 `stepOnce()` 里的 4 个线圈换成 `STEP`/`DIR` 两个引脚即可。

**坑 2：60 秒保持不能让线圈一直通电。**
保持时电机堵转、电流最大，L298N 和电机都会烫。丝杠是自锁的，所以固件默认
`HOLD_DEENERGIZE = true`：**走到位就断电，靠丝杠自锁顶住**，60 秒后直接反向走。
如果发现滑台被动物顶着会往后退，把它改成 `false`（那就必须按坑 1 降压）。

**坑 3：别用 D9/D10 做 analogWrite。**
`Servo` 库占用 Timer1，D9/D10 的 PWM 直接失效。步进只要 `digitalWrite` 开关量，
所以放 D9/D10 没问题。ENA/ENB 反正不接，也就不存在用它们调速这回事。

**坑 4：ENA/ENB 不接 = 跳线帽必须留着。**
L298N 板上 ENA/ENB 各有一个跳线帽，盖着的时候这两个脚被硬拉到 5V，两个 H 桥永远使能，
IN1~IN4 的电平直接决定线圈通不通电。固件里 `PIN_ENA/PIN_ENB = -1`，就是走这条路。
如果跳线帽拔了又没接 ENA/ENB，桥一直处于关断态 —— 电机会**一点反应都没有，而且不报错**，
很容易被误判成"代码没跑/串口没通"，排查时先量一下 IN1~IN4 有没有在翻。

断电（`stepperRelease()`）依然是 `applyCoil(0,0,0,0)`：四路 IN 全低 = 线圈两端同电位
= 没有电流 = 没有保持力矩，效果和接了 ENA/ENB 拉低完全一样。

### 2.4 电源

```
舵机 5V/3A 独立电源 ─┬─ 舵机1 红
                     ├─ 舵机2 红
                     └─ GND ──┐
步进 6~12V ── L298N +12V      │
步进 GND ──── L298N GND ──────┼── Nano GND ── K230 GND   ← 所有 GND 必须连通
Nano 5V ←── USB 或降压模块    │
                              └── K230 自己的电源 GND
```

---

## 3. 机械动作流程（一轮）

**K230 只维护一个电平，下面全部由执行板自己跑：**

```
超声波测距（连采 5 次取中位数）
│
├─ 测不出来（没回波 / 超量程）──> 整轮放弃，什么都不动，等下次触发
│
├─ 有粮（距离 < FOOD_PRESENT_CM）──────────┐
│                                        │
└─ 没粮（距离 ≥ 阈值）                     │
     舵机2 90°→0° 出粮                     │
     保持 10 秒                            │
     舵机2 0°→90° 关闭                     │
          └────────────────────────────────┘
                    │
          舵机1 90°→0°  开门
                    │
          42 步进前进（丝杠滑台把盆推出去）
                    │
          保持 60 秒（动物吃；期间本板不依赖任何外部信号）
                    │
          42 步进退回
                    │
          舵机1 0°→90°  关门
                    │
          断电（IN1~IN4 全低）+ 打印 #=== feed cycle done ===
```

时序（`STEPS_FORWARD=400`、`STEP_US=2000`、`SERVO_MOVE_MS=600` 时）：

| 情况 | 门 | 出粮 | 前进 | 保持 | 退回 | 关门 | 合计 |
|---|---|---|---|---|---|---|---|
| 有粮 | 0.6s | — | 0.8s | 60s | 0.8s | 0.6s | **约 63s** |
| 没粮 | 0.6s | 0.6+10+0.6s | 0.8s | 60s | 0.8s | 0.6s | **约 74s** |

执行板跑完一轮最短约 63s、最长约 74s。这期间它的 `armed` 锁一直关着，
所以 K230 就算一直保持高电平，也不会插进去第二轮。

---

## 4. 触发信号（一根 GPIO）

没有协议、没有命令表、没有回执。就一条规则：

```
K230 检测到动物 -> GPIO42 保持高电平；连续 2 秒没检测到 -> 拉低
执行板看到 低->高 的上升沿 -> 跑一轮
        电平一直高        -> 每 2 分钟再看一眼，还是高就再跑一轮（补粮）
        电平变低          -> 停止补放
```

| 项目 | 值 | 在哪改 |
|---|---|---|
| 信号引脚 | GPIO42（排针第 1 脚） | `k230_trigger.py` 的 `TRIG_PIN` |
| 高电平含义 | 有动物 | `TRIG_ACTIVE_HIGH` |
| 确认"来了" | 连续 3 帧 conf ≥ 0.45 | `PRESENCE_ON_FRAMES` / `PRESENCE_ON_CONF` |
| 确认"走了" | 连续 2000ms 无检测 | `PRESENCE_OFF_MS` |
| 执行板引脚 | D2，`INPUT`（K230 推挽输出，不用上拉） | `simple_trigger.ino` 的 `PIN_TRIG` |
| 补粮周期 | 2 分钟 | `RE_FEED_WHILE_HIGH_MS` |

### 为什么必须消抖（这条最容易被忽略）

YOLO 是逐帧独立的：同一只猫这一帧 `conf=0.62`，下一帧可能 `0.31` 就漏了。
如果直接拿单帧去写 GPIO，电平会疯狂抖：

```
真实:   有有有有有有有有有有有有有 ...
单帧:   有 无 有有 无 有 有 无 有有 ...   <- 一堆假边沿
```

执行板是**靠上升沿**触发的，每个假边沿就是一次多余的放饭（还配一张糊照片）。
所以 K230 侧做了三层：

1. **`PRESENCE_ON_FRAMES = 3`**：连续 3 帧都检测到才算"来了"，单帧噪声进不来。
2. **`PRESENCE_OFF_MS = 2000`**：连续 2 秒都没有才算"走了"。
   猫低头吃粮时头埋在盆里，经常连丢几帧，不能一丢就断电。
3. **`VISIT_MIN_GAP_MS = 30000`**：掉下去又升上来如果不到 30 秒，
   当成同一次抖动，不再开饭。

> 这三个参数和执行板的 `TRIG_STABLE_MS` / `MIN_GAP_MS` 是配套的。改一边要想一下另一边。

### 防误触发 + 补粮（执行板侧）

1. **`TRIG_STABLE_MS = 150`**：高电平必须连续稳定 150ms 才算数，毛刺直接丢。
2. **`BOOT_GUARD_MS = 5000`**：上电前 5 秒不响应（信号线/电源还没稳）。
3. **`armed` 位**：一轮跑完后 `armed = false`，只有等信号**回落到低电平**才重新武装。
   这样"一段连续的高电平"只会被当成一次到访，不会被反复触发。
4. **`RE_FEED_WHILE_HIGH_MS = 120000`**：电平一直高着时，每 2 分钟再看一眼，
   还是高就再跑一轮（补粮）。这一条就是"动物还在吃就继续放"。

第 3、4 条是配合工作的：

```
         上升沿                 一直高                电平回落
动物:  ────┐                              ┌────────────────────
           │                              │
放饭:      ▼        ┌─────┐   2min   ┌─────┐     停止
                   │ 1轮 │ ───────> │ 2轮 │ ──>  等待下次上升沿
                   └─────┘          └─────┘
```

所以同一只猫蹲半小时 ≈ 1 次到访 + 每 2 分钟一次补粮，
而不是只喂一次，也不是疯狂连喂。

### 60 秒保持期间

执行板不需要任何外部信号 —— 它自己 `delay` 过去。所以：

- K230 死机、WiFi 断了、线被拔了，**这一轮照样完整跑完并归位**；
- 不会出现"门一直开着、滑台一直顶着"；
- 也没有心跳超时这种依赖对方的看门狗。

想补回"执行板在忙"这个信息，接一根可选反馈线（见 2.1b）。

---

## 5. K230 侧逻辑（`k230_trigger.py`）

单文件，不用导入本目录的其它模块。

```
每帧: 检测 → 触发区内取最高分框
        ↓
   存在消抖 (连续3帧 conf≥0.45 才算"来了" / 连续2s无检测才算"走了")
        ↓
   GPIO42 = 有动物 ? 高 : 低          ← 一直保持，只在该变的时候变
        ↓
   "没动物"→"有动物" 且距上次开饭 ≥30s  → 开饭（这一刻就是上升沿）
        ↓
   有动物期间，每 60 秒拍一张上传
```

**认出来就喊序号。** 每次命中猫/狗，串口会打一行：

```
[det] 命中 序号=15(cat) conf=0.87 框=212,140,168,190
[det] 命中 序号=16(dog) conf=0.74 框=400,300,120,140
```

序号是从 `LABELS` 里按名字查出来的（`cat` / `dog` 在第几个），所以：
- 标准 COCO 80 类 → **15 / 16**
- 自己训的两类模型 → **0 / 1**
- 不管哪种，**看这行日志就知道实际值**，不用背。

想直接写死序号（比如你确认了就是 14/15），把第 210 行附近改成：

```python
CLASS_IDS_OVERRIDE = (14, 15)     # 原来 None = 按名字自动查
```

#### ⚠️ `postprocess()` 返回的是三元组，不是"一框一行"

这是"模型明明在跑却永远 0 个框"的真正原因，看板载 `libs/YOLO.py` 就明白：

```python
det_res = aidemo.yolov5_det_postprocess(results[0][0], ...)
return det_res          # ← 返回 (框列表, 序号列表, 分数列表)
```

同一个文件的 `draw_result()` 自己就是这样用的：

```python
res[0][i]  ->  x, y, w, h        # 框
res[1][i]  ->  类别序号
res[2][i]  ->  置信度
```

所以**三条平行的列表**，不是每个框一个 6 元组。按 `r[4]` / `r[5]` 去读，
等于在框列表里取第 5 个框再 `float()` 一个元组 —— 必然抛异常，然后被
`except` 吞掉，表现就是"fps 正常、日志干净、框永远是 0"。

`_flatten_detections()` 把两种结构都认了，两种模型都能用，不用改代码。

还有一个容易错的地方：`mode="video"` 时库按 `display_size` 输出坐标，
而触发区按 `RGB888P_SIZE` 算。两者不一致时框会整体错位 ——
代码会自动按比例缩放，并在日志里说明一次。

三个关键设计：

**① 电平只在状态真的变了才写一次。**
不是每帧都写引脚 —— 那样串口会刷满日志，也会掩盖真正的跳变。
`set_level()` 内部记住当前值，只有变化时才动引脚。

**② 一只动物只算一次到访。**
有动物期间电平一直高，执行板没有新的上升沿，就不会重复放饭。
信号掉下去又升上来如果不到 30 秒，当作同一次抖动，不开饭。
不然一只猫在盆边蹭来蹭去会被记成十几次。

**③ 每来一次动物就拍一张，串口和网页数是同一个数。**
`UPLOAD_PER_VISIT = True`（默认）：`gate.visits` 数到访边沿，拍照也数到访边沿，
一一对应。串口打 `第 N 次到访`，网页就有第 N 条记录。

以前是"每 60 秒最多一张"（墙上时钟），那是为了绕开同步上传卡主循环。
现在上传在后台线程，节流没必要了，而且它正是"串口 5 次、网页 1 条"的根源。
想退回旧节奏就设 `UPLOAD_PER_VISIT = False`，再调 `UPLOAD_INTERVAL_MS`。

心跳里会同时打两个数，一眼能看出来对不对得上：

```
[beat] 14.8 fps  框=1  确认=1  次=5  拍=5  看到:dog(0.91),tv(0.22)
                                    ↑到访  ↑拍照
```

失败就落到 `/sdcard/captures`，空闲时自动补传。**上传失败绝不影响开饭**。

### 5.1 "识别不到猫狗"怎么查

以前的代码有两个坑，会让人以为"模型不工作"：

| 坑 | 表现 | 现在怎么处理 |
|---|---|---|
| 类别 ID 硬编码 15/16 | 换成自己训的两类模型（cat=0,dog=1）后，**所有检测结果被过滤光** | 改成按 `LABELS` 名字自动反查，80 类/2 类都能用 |
| 模型加载失败静默退回"模拟检测" | 假猫框让它看起来在识别，其实是编的，还会按假框放饭 | 改成彻底不出结果 + 大字报错误，宁可不喂也不喂假数据 |
| **把 `run()` 的原始张量当成框** | 模型在跑、fps 正常、日志干净，但**框永远是 0**，连"近 5 秒检测到"都不打印 | `run()` 只给张量，必须再调 `postprocess()` 才是框；按形状自动判别 |
| **只验形状不验数值** | 形状像框（长度 6）但 `conf=2.8e+38`、类别 ID 是 30 位天文数字 | 置信度必须在 0~1、宽高为正、类别 ID 在范围内；不合规就判定为张量 |

第三条是最阴的：`model.run(img)` 返回的是原始输出张量（形状像
`(1,144,40,40)`），不是检测框。老代码直接对它取 `r[4]` / `r[5]`，等于在索引
张量的第 5 维 —— 抛异常后被 `except: continue` 吞掉，于是每一帧都很"正常"
地返回空列表。

判断方法不能只看**形状**。实际踩过一个坑：张量切片出来长度正好是 6、
每个数也能转成 float，于是被当成"框"，结果读到的是
`conf=2.80e+38`、类别 ID 是 30 位天文数字。那是把**没写过的内存**
（`0x7F7F7F7F` 按 float32 读 ≈ 3.4e38）当成了检测结果。

所以现在的判定是**形状 + 数值**两道关：

| 检查 | 不合格时的含义 |
|---|---|
| 长度 6、都能转 float | 形状不对 → 是张量，走 postprocess |
| conf 在 0~1 | 概率不可能 >1 → 数值是垃圾，走 postprocess |
| 宽高 > 0、类别 ID 在 0~1000、坐标不是天文数字 | 同上 |

开机时会打印一次 `run()` 的真实返回值，出问题对着看：

```
[det] run() 返回 3 个对象
[det]   out[0] ndarray shape=(1, 144, 40, 40) dtype=float32 len=1
[det]         前 6 个值: 2.8e+38, 2.8e+38, ...
[det] !! run() 的返回值形状像框，但数值是垃圾（conf 远超 1），
     判定为原始张量 -> 改走 postprocess
```

如果 `postprocess` 也救不回来（会打印 `postprocess 的输出数值也不合理`），
去跑 `tools/det_selftest.py`：第 4b 看 `run()` 的结构，第 4e 直接问 KPU
要输出张量的 shape/dtype —— 两段对不上就是库在转 Python 对象时出了问题
（量化模型按 float32 读，就会读出 3.4e38 这种数）。

另外两处影响精度的：

- **宽高比**：AI 帧 4:3、模型输入 1:1。原来按 `fit` 拉伸，猫被横向压扁 1.33 倍，
  远处目标置信度整体掉到阈值以下。现在默认 `RESIZE_MODE = "padding"`（加黑边保持比例）。
- **模型输入尺寸**：显式传 `MODEL_INPUT_SIZE = (320, 320)`，别让库去猜。

还认不出就用现成的排查工具：

```bash
# 三个都在 CanMV IDE 里打开直接运行
# 先分清是"文件不在"还是"模型跑不起来" —— 顺序别反，不然白折腾
tools/list_models.py   # 卡上有哪些 .kmodel、配置的路径在不在
tools/det_probe.py     # 模型能不能跑、每个框为什么不算数
tools/yolo_inspect.py  # run() 到底返回什么、postprocess 该怎么调（见下）
```

#### `tools/yolo_inspect.py` —— 卡死/永远 0 框时跑这个

`libs/YOLO.py` 是板子上明文的 Python 源码。与其猜固件版本，不如直接看它：

| 输出段 | 告诉你什么 |
|---|---|
| `[1]` | 库文件在哪、多大（找不到就是固件不对） |
| `[2]` | `run` / `postprocess` / `draw_result` 的**完整实现** |
| `[3]` | 真跑一次 `run()`，返回值的形状和前几个数 |
| `[4]` | 逐个试 `postprocess` 的 4 种签名，哪个能用 |
| `[5]` | `draw_result` 的用法（官方例程走的就是它） |

⚠️ 如果卡在 `[4]` 某一步不动：拔 USB 重插，然后看卡在第几个 —— 避开那个签名就行。

`k230_trigger.py` 启动时也会自动打印同样的 `[lib]` 源码（只读文件，不会卡），
所以**只跑主程序也能拿到这份信息**。

#### 上传的是整张照片，不是框里那一块

`UPLOAD_FULL_FRAME = True`（默认）：检测到猫狗就把**整帧**压成 JPEG 传上去。

这么做的原因很实际 —— 裁框要用到 `[x, y, w, h]`，而"框偏了 20 像素"和
"照片拍错地方"之间没有任何提示，出问题只能靠眼看。传整帧就完全不依赖框：
哪怕框坐标算法变了，照片也一定是完整的，网页上还能自己判断猫在哪。
代价只是文件大一点（约 40KB → 150KB），局域网和云服务器都不算什么。

网页端不需要任何改动：它本来就只认 `species`（猫/狗）和图片本身，
`box` 一直是可选参数。想看裁出来的效果，把 `UPLOAD_FULL_FRAME` 改回 `False`。

#### 上传失败先看错误码，别猜网络

以前所有上传失败都长这样，看不出原因：

```
[up] 第 1 次失败([Errno 104] ECONNRESET)，重试
[up] 上传失败: [Errno 104] ECONNRESET
```

`ECONNRESET` 是**假象**。真实情况是服务端已经决定拒绝、并且把原因发出去了，
但它没等客户端写完 body 就关连接，内核于是回一个 RST —— 客户端只看到
"连接被重置"，那句有用的 `403 = token 不对` 永远读不到，还白白重试三次。

两端都修好了，现在会直接告诉你原因：

| 日志 | 含义 | 怎么办 |
|---|---|---|
| `403 = token 不对` | 服务端设了 `CATDOG_TOKEN`，K230 没带或带错 | 两边填同一个值；服务端不设，K230 也必须留空 |
| `413 = 服务端拒收这张图` | 超过 `MAX_UPLOAD_BYTES`（默认 4MB） | 基本不会遇到 |
| `400 = not a JPEG/PNG` | 编码坏了 | 看 `[up] !! 这一帧转不成 image.Image` |
| `服务端没有任何响应` | **多数是误判**，不是网络问题，见下 | 靠 uid 去重兜住，不会重复统计 |

另外 403/413 这类错误**不再重试** —— 同一张图、同一个 token，重试还是一样，
只会拖慢主循环。

#### `服务端没有任何响应` 其实是"没等到回包"，图往往已经存进去了

现场日志里三行 `服务端没有任何响应` 之后，服务端库里却多了一张照片 ——
这就是误判。真实链路是这样的：

| 环节 | 实测 |
|---|---|
| K230 连上、发完 100KB | 成功 |
| 服务端收图、特征提取、入库、回 200 | **12~19 ms**（日志里有 `[visit] ... 14 ms`） |
| K230 收到回包 | 没收到 → 8 秒超时 → 判失败 → 重传 |

所以病根在**客户端读响应**这一侧，不是服务端慢，也不是网络断。
两处都改了：

- `_read_status()` 不再把"读不到"当成"对端关闭"。MicroPython 的 socket
  超时可能返回 `None`、也可能抛 `ETIMEDOUT`，现在这两种都算"还没到"，
  继续等，只有真的 EOF(`b""`) 才认输；每次 recv 只等 1 秒，总预算
  `UPLOAD_TIMEOUT_S` 兜住。
- 万一还是没等到，也**不会重复统计**：每张照片带一个 `uid`，
  服务端 `visits.uid` 有唯一索引，重传直接返回原记录（`duplicate: true`），
  既不重复入库，也不重复做特征提取。

一句话：照片少一张无所谓，统计里同一张出现三次才是真的坏。

#### 补传的照片按"拍照时刻"入库

SD 卡里可能躺着一周前没传出去的照片。补传时如果按"服务器收到的时刻"入库，
今天的统计会突然多出几次到访 —— 用户看到的就是"网页数据对不上串口"。

所以补传带 `bf=1`，服务端把 `ts`（板子报的拍照时刻）当作事件时间入库，
并在日志里标 `[补传]`：

```
[visit] k230-01 dog -> 狗-01 (sim=0.912, 老朋友) [补传] 16 ms  uid=k230-01-100211-c3f80873
```

`ts` 只信 2020 年以后的：板子没 RTC 时 `time.time()` 可能是 0 或者从开机
开始算的秒数，把它当时间会得到 1970 年的记录。不可信就退回"收到时刻"。

#### 去重用的 uid 必须按"到访事件"算，不能按照片内容

这条是踩坑踩出来的。相机对着**静止画面**（比如一块显示猫狗照片的屏幕）时，
两次拍照会产出**字节完全相同**的 JPEG。库里 `#10` 和 `#69`（相隔 23 分钟）
sha1 就一模一样。

所以 uid 不能用内容哈希：

| uid 算法 | 后果 |
|---|---|
| 内容哈希 | 两次不同到访被当成重传、去重成一条 → 串口第 2 次到访在网页上消失 |
| 随机数 | 直传超时后补传会带新 uid → 同一张照片存两条 |
| **到访事件**（现在） | 同一次到访的重传/补传 uid 一致；不同到访一定不同 |

现在的 uid 是 `设备号-开机标记-到访号-拍照时刻`，并且**写进 SD 卡文件名**：

```
cat_913_12345_k230-01-89213-1-4512.jpg
         ↑ticks  ↑uid（补传时原样读回来）
```

这样"直传其实成功了但没收到回包"的那张，补传时带的是同一个 uid，
服务端认得，不会多存一条。



#### 照片太大时会自动降质量

640x480 的实拍图在 `JPEG_QUALITY=82` 下可能到 360KB。SDIO WiFi 传一张要好几秒，
日志里能看到 fps 从 15 掉到 11，也更容易被掐断。

现在超过 `UPLOAD_MAX_BYTES`（默认 110KB）就按 `65 → 50 → 40` 逐级降质量重压，
够小立刻停。视觉上几乎看不出差别，传输稳定得多。

想改大一点留着细节：`UPLOAD_MAX_BYTES = 200 * 1024`。

#### 客户端和服务端必须一致的两件事

| 项 | 服务端 | K230 |
|---|---|---|
| token | `CATDOG_TOKEN`（不设 = 不校验） | `API_TOKEN`（必须和它一致，服务端不设就留空） |
| 地址 | `CATDOG_PORT`（默认 8000） | `SERVER_URL`（含端口） |

排查这条链路最省事的办法，是在 PC 上用板子同样的写法打一遍：

```bash
python tools/sim_k230_upload.py
```

它会用 `HTTP/1.0 + 裸 socket` 发普通大小、超大、错误 token 三种请求，
直接告诉你服务端给的是可读响应还是掐断连接。

> **它会往库里写测试数据**：`device=k230-sim`，照片是随机噪声图。
> 所以默认**拒绝**打生产库（会去 `/api/v1/health` 确认用的是哪个 db 文件）。
> 压测要么换个测试端口，要么加 `--allow-prod`。真写进去了就跑
> `python tools/prune_visits.py --apply` 清掉。

`det_probe.py` 会把模型**看到的东西**原样打出来（不过滤、不消抖、不写 GPIO），
每个框后面直接标注为什么不算数：

```
cat 0.87  box=(212,140,168,190)  中心(296,235)  <- OK 会触发
person 0.55  box=(10,20,300,400)              <- 不是猫狗，会被丢掉
dog 0.33  box=(400,300,120,140)               <- [LOW] 低于判定门槛 0.40
cat 0.72  box=(10,380,90,90)                  <- 中心在触发区外
```

按这个对症：
- **一个框都没有** → 模型没加载 / kmodel 和固件不匹配
- **有框但全是 person/car** → `LABELS` 和 kmodel 类别顺序对不上
- **有 cat/dog 但都很低分(0.2~0.4)** → 把 `CONF_THRESH` 降到 0.30，或改善光照
- **有高分猫狗但不触发** → `TRIGGER_ZONE` 设错了

如果连模型都加载不起来（串口那行 `Kmodel file not exist.`），先跑 `tools/list_models.py`：

```
!! 卡上实际存在的 .kmodel（2 个）:
!!   /sdcard/kmodel/yolov8n_320.kmodel        (3.5 MB)
!!   /sdcard/kmodel/yolov8n_224.kmodel        (3.3 MB)
!! -> 把 KMODEL_PATH 改成上面真正存在的那个路径
```

> 出厂卡上现成就有 `/sdcard/kmodel/yolov8n_320.kmodel`，默认配置已经指向它，
> 正常情况下这一步不需要你做任何事。

> 注意 `MODEL_INPUT_SIZE` 必须和模型真的输入尺寸一致。80 类 COCO 的
> `yolov8n.kmodel` 多数是 320x320；如果拿到的是 640 的，把
> `MODEL_INPUT_SIZE` 改成 `(640, 640)`，否则**不报错但框全错位**。

取舍清单见 1 节末尾「这个方案放弃了什么」。

---

## 6. PC 端（`pc/`）

### 6.1 启动

```bash
python pc/run.py
```

```
==============================================================
 流浪猫狗放饭 · PC 服务端
==============================================================
 本机访问 : http://127.0.0.1:8000
 局域网   : http://192.168.1.100:8000
 数据目录 : D:\...\pc\data
==============================================================
```

把打印出来的局域网 IP 填进 K230 的 `k230_trigger.py`：

```python
SERVER_URL = "http://192.168.1.100:8000"
```

浏览器打开 `http://<那个IP>:8000` 就是统计页面。

**不需要 pip 安装任何东西**：HTTP 服务、数据库、JSON 全用标准库；
图像处理用 Pillow（一般都有）；个体识别有 torch/torchvision 就自动用，没有就退回手工特征。

没有硬件也想先看效果：

```bash
python tools/demo_data.py --yes      # 塞 30 条假到访
python pc/run.py
```

### 6.2 网页能看什么

- **总到访 / 今天到访 / 猫狗次数 / 独立个体数**
- **24 小时到访分布**：猫、狗分开配色，一眼看出是凌晨来的还是傍晚来的
- **时段汇总**：深夜 / 清晨 / 上午 / 中午 / 下午 / 傍晚 / 夜晚
- **个体卡片**：每只猫/狗的照片、名字、来了几次、最近一次是什么时候
  - 点名字 → 改名（"橘猫老大"）
  - 点"合并…" → 把认错的两只并成一只，次数立刻合并
- **最近到访**：时间、个体、物种、相似度、缩略图（点开看大图）

### 6.3 个体识别是怎么做的（以及它的上限）

```
照片 → ResNet18(ImageNet, 去掉分类头) → 512 维向量 → 归一化
                                              ↓
                       和库里同物种的所有向量算余弦相似度
                                              ↓
              最高分 ≥ MATCH_THRESHOLD(默认 0.82) ? 老朋友 : 新建个体
```

- 每个个体最多留 12 个向量（`MAX_EMB_PER_INDIVIDUAL`），越多越不容易漏认；
- 阈值在 `pc/config.py` 的 `MATCH_THRESHOLD`，**一定要按你的实际画面调**：
  先设成 0.99（几乎不合并）跑几天，看网页上"相似度"列的分布，
  再把阈值卡在"同一只"和"不同只"之间；
- 网页上每条到访都显示了相似度，方便你判断。

**说清楚局限**：单摄像头 + 一张图，个体识别不可能 100%。

| 场景 | 结果 | 缓解 |
|---|---|---|
| 同一只猫不同姿态/光照/远近 | 相似度可能掉到 0.7 以下 → 被当成新个体 | 网页"合并"一键修好 |
| 两只花色体型接近的猫 | 相似度反而很高 → 被并成一只 | 网页"合并"到反方向 / 改名后重新观察 |
| 夜里只有红外黑白画面 | 颜色信息没了，只有轮廓 | 夜间识别本来就不准，看次数趋势够用 |
| 镜头里出现新来的猫 | 自动建 `猫-03` | 正常，改个名字即可 |

**想显著提高准确率**，性价比最高的办法不是换模型，是**给每只猫戴不同颜色的项圈**。
代码里预留了位置：`pc/server.py` 的 `handle_visit()` 里拿到 `rgb` 之后，
可以先做一个"颜色块检测"，命中项圈颜色就直接定个体，不走特征匹配。

### 6.4 接口（想自己接别的东西时用）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/visit?device=&species=&conf=&box=&ws=&hs=&ts=` | body 是原始 JPEG（也支持 multipart） |
| GET | `/api/v1/overview` | 网页用的全部数据 |
| GET | `/api/v1/visits?limit=50&individual_id=` | 最近到访 |
| POST | `/api/v1/individual/<id>/rename` | `{"name":"小黑"}` |
| POST | `/api/v1/individual/<id>/merge` | `{"into":3}` |
| POST | `/api/v1/visit/<id>/assign` | `{"individual_id":3}` |
| GET | `/api/v1/health` | 后端/阈值/数据库路径 |
| GET | `/img/<日期>/<文件>.jpg` | 图片 |

手测一张：

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/visit?species=cat&conf=0.9&device=manual" \
     -H "Content-Type: image/jpeg" --data-binary @cat.jpg
```

数据都在 `pc/data/`：`visits.db`（SQLite）+ `images/日期/xxx.jpg`。备份直接拷这个目录。

### 6.5 放到云端（K230 和后台不在同一局域网）

**先回答：可以，而且不用改架构。** 原因很简单 ——
**K230 永远是主动发起连接的一方**，后台只是被动收 HTTP 请求：

```
   家里                                    云端
┌──────────┐   K230 主动连出去    ┌──────────────────┐
│  K230    │ ──────────────────> │ 公网 IP / 域名    │
│ (NAT 后面)│    HTTPS POST 照片   │  nginx → 服务端   │
└──────────┘                     └──────────────────┘
```

所以**不需要在家里路由器上做端口映射**，也不需要公网 IP。
换成公网地址只是把 `SERVER_URL` 改一行。

但有三件事必须做，否则会静默失败：

#### ① 必须设 token（否则任何人都能往你库里塞照片）

```bash
# 服务端
set CATDOG_TOKEN=一串足够长的随机字符
python pc/run.py
```

```python
# K230：必须和服务端完全一致
API_TOKEN = "一串足够长的随机字符"
```

K230 会把 token 拼在 URL 上（`?token=xxx`），和服务端 `_check_token` 对得上。
**对不上就是 403** —— 照片只会存进 SD 卡，网页上永远看不到。
（旧版代码压根没发 token，一旦服务端开了鉴权就必然 403，已修。）

> token 只允许字母数字和 `- _ .` —— K230 上没有 urlencode 可用。

#### ② 用 HTTPS，别裸奔 HTTP

照片走公网明文 HTTP 等于谁都能看。两种做法：

| 做法 | 怎么做 | 推荐度 |
|---|---|---|
| **反代做 TLS** | nginx / Caddy / 云厂商 SLB 监听 443，转发到本服务的 8000 | ⭐ 推荐 |
| 服务端自己开 TLS | `set CATDOG_TLS_CERT=fullchain.pem` + `set CATDOG_TLS_KEY=privkey.pem` + `set CATDOG_PORT=443` | 没有反代时用 |

```python
# K230
SERVER_URL = "https://你的域名"
SSL_VERIFY = False    # CanMV 固件一般没有 CA 证书包，设 True 会连不上
```

> `SSL_VERIFY = False` 意味着不校验证书，防不了中间人。
> 照片是流浪猫狗，风险可以接受；真要严格就自己把 CA 放到 `/sdcard` 并改 `_tls_wrap`。
> 如果你的固件连 `ssl` 模块都没有，K230 会明确打印"用不了 https"，不会静默失败。

#### ③ 超时和重试要调大

```python
UPLOAD_TIMEOUT_S = 8     # 原来是 2s，只够局域网
UPLOAD_RETRIES = 2
```

家里上行带宽小的时候，传一张裁剪图（几十 KB）就要好几秒。
2 秒必然大量失败，然后照片全堆到 SD 卡里。

#### 云端要注意的其它几点

| 事项 | 说明 |
|---|---|
| **安全组/防火墙放行** | 云服务器默认只开 22/80/443。`8000` 要么加规则，要么用反代只暴露 443 |
| **域名解析** | `SERVER_URL` 可以直接写域名，K230 的 socket 会做 DNS |
| **后台挂子路径** | `https://example.com/catdog` 也支持，`parse_server_url()` 会拆出 base path |
| **数据别放容器里** | `CATDOG_DATA_DIR` 指到挂载卷，否则重建容器照片就没了 |
| **时间戳按到达时间** | 服务端用 `time.time()` 记到访时间。断网期间存 SD 卡的照片补传时，时间会是补传那一刻，不是拍的那一刻（K230 没有 RTC） |
| **网页要能被访问** | 页面没有任何鉴权（方便你看）。公网上建议只给反代加一层 Basic Auth，或者用内网/VPN |

一个最小的 Caddy 反代例子（自动申请证书）：

```
catdog.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

### 6.6 部署到阿里云 ECS

`deploy/` 目录里已经放好三个文件：`requirements.txt`、`catdog.service`、`nginx-catdog.conf`。

#### ① 买机器

| 项 | 建议 |
|---|---|
| 规格 | **2 核 2G** 起步（Pillow 解码 + 缩略图吃内存，1G 容易 OOM） |
| 系统盘 | 40G 够用，但**照片会一直长**，建议单独挂一块数据盘到 `/var/lib/catdog` |
| 系统 | Ubuntu 22.04 或 Alibaba Cloud Linux 3 |
| 地域 | 见下面「备案」—— 这是最容易卡住的一步 |

#### ② ⚠️ 备案（大陆地域绕不过去）

**ECS 在中国大陆地域时，域名走 80/443 必须先做 ICP 备案**，否则会被运营商拦截，
表现是"域名 ping 得通、网页打不开"。三条路选一条：

| 选择 | 代价 |
|---|---|
| 老老实实备案 | 免费，但要等 7~20 天，需要域名实名 |
| ECS 买**香港**地域 | 不用备案，但大陆访问延迟高一些，K230 上传时间会变长 |
| 不买域名，直接用 `http://公网IP:8000` | 也得开 8000 端口，且**没有 HTTPS**，照片明文过公网 |

> 只是自己用、照片是流浪猫狗的话，第三条其实能接受 —— 但请务必设 `CATDOG_TOKEN`。

#### ③ 安全组（只开三个口）

阿里云控制台 → 实例 → 安全组 → 入方向，加规则：

| 端口 | 来源 | 用途 |
|---|---|---|
| 22 | 你的 IP（别用 0.0.0.0/0） | SSH |
| 80 | 0.0.0.0/0 | 申请证书 / 跳转 https |
| 443 | 0.0.0.0/0 | 真正的入口 |

**不要开 8000** —— 服务绑在 `127.0.0.1`，只让 nginx 转发（默认配置就是这样）。

#### ④ 上传代码

只跑网页后台的话，`pc/` 这一个目录就够了（K230 固件、Arduino 代码不用传）：

```bash
# 在你 Windows 上（PowerShell）
ssh root@你的公网IP "mkdir -p /tmp/catdog-src"
scp -r pc deploy root@你的公网IP:/tmp/catdog-src/
```

或者用 WinSCP 拖过去。

#### ⑤ 装依赖

```bash
sudo useradd -r -s /usr/sbin/nologin catdog
sudo mkdir -p /opt/catdog /var/lib/catdog

# 注意别写成 "mv /tmp/catdog-src/pc /opt/catdog" —— 那会把 pc 改名成 catdog，
# 而 ExecStart 找的是 /opt/catdog/pc/run.py
sudo mv /tmp/catdog-src/pc     /opt/catdog/pc
sudo mv /tmp/catdog-src/deploy /opt/catdog/deploy

sudo chown -R catdog:catdog /opt/catdog /var/lib/catdog

cd /opt/catdog
sudo python3 -m venv .venv
sudo .venv/bin/pip install -U pip
sudo .venv/bin/pip install -r deploy/requirements.txt
```

> 想要更好的个体识别精度再装 `torch torchvision`（约 +800MB，2G 内存跑起来很吃力）。
> 不装也行 —— 会自动退回 numpy 手工特征，能区分明显的花纹差异，但同窝小猫会分错。

#### ⑥ 配 token + 开机自启

```bash
sudo mkdir -p /etc/catdog
sudo cp /opt/catdog/deploy/env.example /etc/catdog/env
sudo nano /etc/catdog/env        # 把 CATDOG_TOKEN 换成 openssl rand -hex 24 的产物
sudo chown root:catdog /etc/catdog/env
sudo chmod 640 /etc/catdog/env

sudo cp /opt/catdog/deploy/catdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now catdog
sudo systemctl status catdog
journalctl -u catdog -f          # 看日志
```

看到 `流浪猫狗放饭 · PC 服务端` 和监听地址就对了。

#### ⑦ 配 HTTPS

去阿里云「数字证书管理服务」申请**免费 DV 证书**（单域名、免费、一年）。
签发后下载 **Nginx 格式**，得到 `.pem`（含链）和 `.key`：

```bash
sudo mkdir -p /etc/nginx/ssl
# 把两个文件传上来，改好文件名后：
sudo nano /etc/nginx/conf.d/catdog.conf     # 内容用 deploy/nginx-catdog.conf，改 server_name 和证书路径
sudo nginx -t && sudo systemctl reload nginx
```

#### ⑧ 改 K230

```python
# k230_trigger.py
SERVER_URL = "https://catdog.example.com"
API_TOKEN  = "和 /etc/catdog/env 里 CATDOG_TOKEN 一模一样的值"
UPLOAD_TIMEOUT_S = 8      # 跨省公网，8 秒比较稳
```

#### ⑨ 验收（按顺序）

```bash
# 1. 服务端活着
curl -s https://catdog.example.com/api/v1/health

# 2. 不带 token 必须被拒（这一步很关键，返回 200 说明鉴权没生效）
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  "https://catdog.example.com/api/v1/visit?species=cat&device=test" \
  -H "Content-Type: image/jpeg" --data-binary @test.jpg
# 期望 403

# 3. 带 token 应该 200
curl -s -X POST \
  "https://catdog.example.com/api/v1/visit?species=cat&device=test&token=你的token" \
  -H "Content-Type: image/jpeg" --data-binary @test.jpg

# 4. 网页能看到刚才这张
```

最后让 K230 对着镜头晃一下，看它的串口有没有 `[up] sent`。

#### 阿里云上几个容易踩的坑

| 现象 | 原因 |
|---|---|
| 网页能开，上传 413 | nginx 的 `client_max_body_size` 没改（默认 1M） |
| 域名 ping 通但打不开 | 大陆地域没备案 |
| 上传偶发超时 | 跨地域延迟 + 家里上行小，把 `UPLOAD_TIMEOUT_S` 调到 10 |
| 重启后照片全没了 | `CATDOG_DATA_DIR` 指到了系统盘，或没挂数据盘 |
| 服务起不来，日志说 Permission denied | `/var/lib/catdog` 的属主不是 `catdog` |
| 内存吃满被 OOM kill | 2G 以下机器又装了 torch；卸载 torch 或升配置 |

### 6.7 连不上怎么办：在 K230 上跑一次自检

云端那边的失败几乎都是**静默**的 —— 检测、开饭、画框全都正常跑，
只有照片悄悄进了 SD 卡。串口上就一句 `[up] 上传失败`，看不出是哪一环。

所以 `tools/cloud_check.py` 把整条链路拆成 7 段，逐段判定：

```
[1/7] WiFi           已连接  ip=192.168.43.170
[2/7] TLS 支持       固件有 ssl 模块
[3/7] DNS 解析       catdog.example.com -> 47.x.x.x
[4/7] TCP 连接       通  (38 ms)
[5/7] TLS 握手       握手成功  (621 ms)
[6/7] GET  /health   200 服务活着
[7/7] POST /visit    不带 token -> 403 鉴权生效（正确）
                     带 token -> 200  **上传链路通了**
```

用法：`k230_trigger.py` 里的 `SERVER_URL` / `API_TOKEN` 填好之后，
CanMV IDE 直接运行 `tools/cloud_check.py`（不开相机、不占 KPU，和主程序互不影响）。
它读的就是主程序的配置，所以**测的就是实际在用的值**，不存在"测的时候通、跑起来不通"。

失败时它不只报数字，还会直接说该改什么：

| 现象 | 它给出的结论 |
|---|---|
| 第 4 步不通 | 按可能性列出：安全组端口 / 服务没起 / 只绑了 127.0.0.1 / 出站被挡 |
| 第 5 步失败 | 证书域名不符、或者对端根本不是 HTTPS |
| 带 token 还是 403 | token 两边一字不差地对比 |
| 404 | `SERVER_URL` 的子路径和 nginx 的 `location` 对不上 |
| 413 | nginx 的 `client_max_body_size` |
| 502 / 504 | nginx 连不上后端（服务没起，或 `CATDOG_HOST` 绑错了） |

**主干程序开机时也会自检一次**（连上 WiFi 立刻打印，不耗时、不联网）：

```
[up] 目标 https://catdog.example.com:443/
[up] https 可用（ssl 模块在）
[up] !! 地址看着像局域网，但不在同一网段：
     后台 192.168.1.100  /  本机 192.168.43.170
```

最后那条是最常见的错：`SERVER_URL` 还留着示例的 `192.168.1.100`，
而板子连的是手机热点 `192.168.43.x` —— 两个网段根本不通。

---

## 7. 目录结构

```
── K230 侧（CanMV MicroPython，根目录）──────────────
k230_trigger.py    ★ K230 跑这个：识别 + 上传 + 拉高引脚（单文件）
config.py          视觉参数（相机/WiFi/PC 地址/阈值）
detector.py        YOLO kmodel 适配层
zones.py           触发区 / 裁剪框几何计算
food_vision.py     老版本的视觉残食率（已不参与决策，留作对比）
── 执行板 ────────────────────────────────────────
arduino/simple_trigger/simple_trigger.ino  ★ 舵机/步进/超声测粮/60s 保持（单根 GPIO 触发）
── PC 端 ────────────────────────────────────────
pc/run.py          启动入口
pc/server.py       HTTP 接口 + 上传处理
pc/db.py           SQLite（individuals / visits / embeddings）
pc/identity.py     特征提取 + 个体匹配
pc/web/index.html  网页（单文件，无外部依赖，断网也能开）
── 工具 ─────────────────────────────────────────
tools/gpio_probe.py    逐个探测 GPIO 能不能输出（接 LED 量）
tools/demo_data.py     塞假数据预览网页
tools/vision_probe.py  饭盆 ROI / 粮量标定
tools/camera_probe.py  相机通道排查（snapshot chn(2) failed 时用）
tools/list_models.py   模型文件体检（报 Kmodel file not exist 时先跑这个）
tools/det_probe.py     识别排查（"能跑但识别不到猫狗"时用，逐框标注为什么不算数）
tools/det_selftest.py  识别链路分段自检（加载/推理/后处理，各段单独计时）
tools/cloud_check.py   ★ 云端连通性自检（WiFi/DNS/TCP/TLS/token/上传，逐段判定）
── 云端部署（阿里云 ECS）──────────────────────────
deploy/requirements.txt    服务端依赖（Pillow / numpy）
deploy/catdog.service      systemd 开机自启 + 崩溃自愈
deploy/nginx-catdog.conf   nginx 反代 + HTTPS
deploy/env.example         /etc/catdog/env 模板（token 放这里，别写进 unit）
── 测试 ─────────────────────────────────────────
tests/k230_trigger_sim.py      K230 侧逻辑仿真（脉冲/一只动物只喂一次/URL 解析/token/补传）
tests/arduino/                 Nano 固件自检（g++ 跑逻辑 + avr-g++ 真编译）
tests/pc/test_server.py        PC 服务端端到端测试
train/README.md                数据集 / 训练 / 转 kmodel
```

> 标 ★ 的两个文件就是整套系统要烧/要跑的东西。其余是它们依赖的视觉模块和 PC 端。

---

## 8. 上板步骤（按这个顺序，能少走很多弯路）

### 第 1 步：单独调执行板（不接 K230）

烧 `arduino/simple_trigger/simple_trigger.ino`，插 USB 看 `Serial` 日志（115200）。

因为没有串口下发命令，调试方式就是**改常数 → 重烧 → 看动作**。
先改这几个量再烧：

```cpp
const int DOOR_CLOSED   = 90;   // 你的门关上时舵机是多少度
const int DOOR_OPEN     = 0;
const int HOPPER_CLOSED = 90;
const int HOPPER_OPEN   = 0;
const long STEPS_FORWARD = 400; // 推出行程：量一下多少毫米
const unsigned int STEP_US = 2000;
```

然后**逐个功能单独试**。没有串口命令，所以是改一个常数、烧一次、看一次动作：

| 要确认的 | 怎么试 |
|---|---|
| 机械在初始位置 | 上电后门=90°关、粮仓=90°关、滑台在最里侧 |
| 门的方向 | 临时把 `DOOR_OPEN` 改成 90 烧一次，看门是不是真的开 |
| 步进方向 | 把 `STEPS_FORWARD` 改成 `-400` 烧一次（前进 = 盆往外） |
| 推出行程 | 用上面的行程量实际推出去多少毫米，再折算 `STEPS_FORWARD` |
| 超声波读数 | 上电会打 `#boot distance=xx.x cm`；空盆和装满各测一次，差值应 ≥3cm |
| 粮量阈值 | 把 `FOOD_PRESENT_CM` 定在上面两次读数中间，重烧 |
| 出粮量 | 改 `FEED_HOPPER_MS`（默认 10000） |
| 完整一轮 | 把 `FEED_HOLD_MS` 临时改成 8000，别等 60 秒 |

> `STEP_US` 越小越快，但太小会丢步（电机嗡嗡响但不转就是丢步了）。
> 2000µs ≈ 500 步/秒 ≈ 2.5 圈/秒。

### 第 2 步：验证触发（K230 还没上）

用一段导线把 D2 碰一下 5V（或直接 `digitalWrite` 一个测试脚），模拟 K230 的高电平：

- 碰一下（≥150ms）→ 应该跑完整一轮；
- 碰一下立即松开（<150ms）→ 应该什么都不做（毛刺被丢）；
- 一直按着不放 → 只跑一轮，不会反复开饭（靠 `armed` 位）。

也可以用 `SIMULATE_DETECTION = True` 跑 `k230_trigger.py`，
让它自己每来一只"假猫"就拉高一次，直接联调。

### 第 3 步：跑通 PC 端

```bash
python pc/run.py
```

记下打印的局域网 IP，填进 K230 的 `config.py`。
先在浏览器里打开确认页面出来了。

### 第 4 步：K230 连 WiFi + 上传

```python
# k230_trigger.py
WIFI_SSID = "你的WiFi"
WIFI_PASS = "密码"
SERVER_URL = "http://192.168.1.100:8000"   # 第 3 步记下的 IP
UPLOAD_MODE = "wifi"
```

跑 `k230_trigger.py`，用手在镜头前晃一下（或者直接 `SIMULATE_DETECTION = True`），
看 PC 那边的网页有没有出现照片。

> Windows 防火墙会挡 8000 端口。第一次运行会弹窗，选"允许访问"。
> 没弹窗就手动放行：`netsh advfirewall firewall add rule name="catdog" dir=in action=allow protocol=TCP localport=8000`

### 第 5 步：不接模型先跑一遍状态机

```python
SIMULATE_DETECTION = True    # 用一个假猫框走完整流程
```

确认：检测到 → GPIO 保持高 → 板子动 → 60 秒 → 收回 →（动物走了才拉低）。

### 第 6 步：上模型

**不用准备任何文件** —— 亚博/CanMV 出厂固件自带 80 类 COCO 的 yolov8n：

```
/sdcard/kmodel/yolov8n_320.kmodel      3,650,464 字节
```

`config.py` / `k230_trigger.py` 的默认 `KMODEL_PATH` 已经指向它。
实测 CanMV v1.4.3 k230_canmv_yahboom：`kpu.load_kmodel()` 成功，
输入 1 个张量、输出 4 个张量，和 `libs.YOLO.YOLOv8` 的接口完全对得上。

所以这一步只要：

1. 确认串口有 `[det] 模型加载完成: /sdcard/kmodel/yolov8n_320.kmodel`；
2. 跑起来看有没有 `[count] 猫 +1`；
3. 万一路径不对，跑一次 **`tools/list_models.py`**，它会把卡上所有 `.kmodel`
   连路径带大小列出来（加载失败时程序也会自动列出这份清单，并自动改用一个存在的）。

> ⚠️ 目录名是 `kmodel` 不是 `models`。以前文档写的 `/sdcard/models/` 在出厂卡上
> **不存在**，这就是 `Kmodel file not exist.` 的来源。
>
> COCO 模型对夜间、逆光、远处、半张脸的流浪猫召回率会掉。
> 先按这个把机械跑通，再决定要不要自己拍数据重训（见 `train/README.md`）。

### 第 7 步：整机联调

正常日志长这样：

```
[main] 信号引脚 GPIO42（亚博 12pin 排针第 1 脚）
[main]   检测到猫狗 -> 一直高电平；连续 2.0s 没检测到 -> 拉低
[main]   消抖: 连续 3 帧 conf>=0.45 才算来了
[main]   补粮节奏由执行板自己控制（一直高电平就定时补），K230 不管
[main]   拍照上传: 每 60 秒最多 1 张
[main] 上传: mode=wifi ready=True
[gate] 检测到动物 -> 信号线拉高
========== 第 1 次到访 -> 拉高电平（猫 conf=0.91）==========
    执行板会跑一轮；它不退走的话，执行板自己每 2 分钟补放一次
[up] 拍照: cat conf=0.91 320x280 18234B
[trig] GPIO42 -> 高（检测到动物）
[visit] k230-01 cat -> 橘猫老大 (sim=0.912, 老朋友)
```

同时 Arduino 那边（插着 USB 看到的）：

```
#boot distance=18.4 cm            ← 上电自检一次
#-> set FOOD_PRESENT_CM between (full) and (empty)
READY
...
#dist=18.4 cm                    ← 每轮开饭前都测一次
#=== feed cycle start ===
#food=0 (hopper first)
#hopper 出粮
#door open
#bowl out
...（60 秒后）
#bowl in
#door close
#=== feed cycle done ===
```

测距失败时是这样（**什么都不动**）：

```
#dist=-- cm (no echo)
#=== cycle SKIPPED: ultrasonic no echo / out of range ===
#    check: probe wiring, probe facing the bowl, bowl deeper than 2cm
#    (nothing was moved: door closed, no feed, stepper still)
```

---

## 9. 参数速查

### K230 侧 `k230_trigger.py`

| 想改什么 | 改哪个 |
|---|---|
| 触发引脚 | `TRIG_PIN`（默认 42 = 排针第 1 脚） |
| 高电平含义 | `TRIG_ACTIVE_HIGH`（默认 True = 有动物为高） |
| 确认"来了" | `PRESENCE_ON_FRAMES`（默认 3 帧）/ `PRESENCE_ON_CONF`（默认 0.45） |
| 确认"走了" | `PRESENCE_OFF_MS`（默认 2000ms） |
| 掉下去又升上来的最短间隔 | `VISIT_MIN_GAP_MS`（默认 30000） |
| 上传间隔 | `UPLOAD_INTERVAL_MS`（默认 60000 = 1 分钟） |
| 只有动物时才传 | `UPLOAD_ONLY_WHEN_PRESENT`（默认 True） |
| 启动时打印引脚状态 | `PRINT_PIN_INFO`（默认 True） |
| 可选 BUSY 反馈 | `BUSY_PIN`（默认 -1 = 没接） |
| 检测置信度门槛 | `CONF_THRESH`（默认 0.40）/ `NMS_THRESH` |
| 模型输入尺寸 | `MODEL_INPUT_SIZE`（默认 320×320，要和 kmodel 一致） |
| 预处理方式 | `RESIZE_MODE`（默认 `padding`，别改成 `fit`，会把猫压扁） |
| 类别名（要和模型一致） | `LABELS` —— cat/dog 的下标会自动从里面反查 |
| PC 地址 | `SERVER_URL`（如 `http://192.168.1.100:8000` 或 `https://域名`） |
| 上传鉴权 | `API_TOKEN`（要和服务端 `CATDOG_TOKEN` 一致） |
| 上传超时/重试 | `UPLOAD_TIMEOUT_S`（默认 8）/ `UPLOAD_RETRIES`（默认 2） |
| 是否校验 TLS 证书 | `SSL_VERIFY`（默认 False，CanMV 一般没有 CA 包） |
| WiFi | `WIFI_SSID` / `WIFI_PASS` |
| 不上传 / 只存 SD | `UPLOAD_MODE = "off"` / `"sd"` |
| 触发范围 | `TRIGGER_ZONE`（**像素**，坐标系 = `RGB888P_SIZE`，改分辨率要跟着改） |
| AI 取图分辨率 | `RGB888P_SIZE`（默认 640×480，宽度须 16 对齐） |
| 屏幕分辨率 | `DISPLAY_SIZE`（亚博 2.4 寸 = 640×480，必须和实物一致） |
| 显示方式 | `DISPLAY_MODE`（`lcd` / `hdmi` / `virt` / `auto`） |
| 裁剪时外扩多少 | `CROP_PAD` |
| 认错/漏检 | `CONF_THRESH` |
| 假检测（不接模型） | `SIMULATE_DETECTION` |

> `config.py` 里只剩相机 / WiFi / PC 地址 / 阈值这些**视觉参数**，
> 供 `detector.py` 和 `tools/vision_probe.py` 用。`k230_trigger.py` 是单文件的，
> 触发和时序参数都在它自己顶部。

### 执行板 `simple_trigger.ino`

| 想改什么 | 改哪个 |
|---|---|
| 触发脚 | `PIN_TRIG`（默认 D2） |
| 可选 BUSY 输出 | `PIN_BUSY`（默认 -1 = 没接） |
| 超声波脚 | `PIN_US_TRIG` / `PIN_US_ECHO`（默认 D11 / D12） |
| **粮量阈值** | `FOOD_PRESENT_CM`（默认 12.0，小于它算有粮） |
| 取样次数/下限 | `US_SAMPLES` / `US_MIN_VALID`（默认 5 / 3） |
| 盲区/量程 | `US_VALID_MIN_CM` / `US_VALID_MAX_CM`（默认 2.0 / 60.0） |
| 测距失败兜底 | `US_FAIL_FALLBACK_AFTER`（默认 0 = 放弃本轮） |
| 舵机角度 | `DOOR_CLOSED/OPEN` / `HOPPER_CLOSED/OPEN` |
| 舵机速度 | `SERVO_MOVE_MS` |
| 步进行程 | `STEPS_FORWARD`（步数 = 毫米 / 导程 × 200） |
| 步进速度 | `STEP_US` |
| 半步/整步 | `STEP_HALF` |
| 60s 保持是否断电 | `HOLD_DEENERGIZE`（见 2.3 坑 2） |
| 出粮/保持时长 | `FEED_HOPPER_MS` / `FEED_HOLD_MS` |
| 限位开关 | `PIN_LIMIT_OUT` / `PIN_LIMIT_IN`（默认 -1 = 没装） |
| 防毛刺 | `TRIG_STABLE_MS`（默认 150） |
| 上电保护 | `BOOT_GUARD_MS`（默认 5000） |
| 上升沿触发的最小间隔 | `MIN_GAP_MS`（默认 30000） |
| **持续高电平的补粮周期** | `RE_FEED_WHILE_HIGH_MS`（默认 120000 = 2 分钟） |
| 调试打印 | `DEBUG_SERIAL`（默认 true） |

### PC 侧 `pc/config.py`

| 想改什么 | 改哪个 |
|---|---|
| 端口 / 监听地址 | `PORT` / `HOST` |
| 个体识别严格程度 | `MATCH_THRESHOLD`（越大越不容易合并） |
| 特征后端 | `EMBED_BACKEND = "auto"/"torch"/"handcrafted"` |
| 每个个体存几个特征 | `MAX_EMB_PER_INDIVIDUAL` |
| 缩略图大小 | `THUMB_WIDTH` |
| 上传鉴权 | `API_TOKEN`（K230 URL 加 `&token=xxx`） |

---

## 10. 自检 / 仿真（不用硬件）

```bash
# K230 侧逻辑：电平消抖、一只动物只喂一次、走了再来、类别映射、URL/token、补传
python tests/k230_trigger_sim.py

# PC 服务端：上传、认个体、统计、改名、合并、取图、错误处理
python tests/pc/test_server.py

# Nano 固件：把真 .ino 用 g++ 编出来，喂触发信号看动作
g++ -std=c++11 -Wno-trigraphs -I tests/arduino \
    -o tests/arduino/simple_trigger_test.exe tests/arduino/simple_trigger_test.cpp
tests/arduino/simple_trigger_test.exe

# Nano 固件：按真板子（ATmega328P）编译一遍，顺便报 flash / RAM 占用
powershell -ExecutionPolicy Bypass -File tests/arduino/build_nano.ps1
```

> **最后那条别省。** 上面那个 g++ 自检跑在 PC 上：`int` 是 32 位、内存无限、
> 引脚号也不受板子限制 —— 它能过，不代表 Nano 能过。
> `build_nano.ps1` 用的是真正的 `avr-g++` + AVR 核心库，能抓到 PC 自检抓不到的问题：
> RAM 超 2KB（报错很隐晦，现场表现是"跑一会儿莫名重启"）、flash 超 30.7KB、
> 以及只有 16 位 `int` 才会暴露的溢出。
>
> 当前基线：**flash 30.6%（10036B）、静态 RAM 36.0%（737B）**。
> 静态 RAM 超过 1200B 脚本会警告 —— 那时栈就没地方了。

改了对应模块就先跑对应的自检，能省掉大量"插拔 USB + 盯着板子看"的时间。
桩里的 `delay()` 只是把时钟往前推，所以 63 秒的一轮在 PC 上瞬间跑完。

### 查照片到底拍没拍到（`tools/check_photos.py`）

```bash
python tools/check_photos.py            # 检查全部存下来的照片
python tools/check_photos.py --last 10  # 只看最近 10 张
```

**为什么要单独有这个工具**：串口一直刷 `[up] 上传成功`、网页记录一条不少、
文件也有几十 KB —— 一切看起来都对，但照片可能**是一片灰**。

原因是 `PipeLine.get_frame()` 返回的帧是 **RGB888 平面格式**（官方文档写的是
`Sensor.RGBP888`），把它当成打包 RGB888 去压缩，存出来就是灰的。这种失败
不报错、不丢记录，光看日志发现不了。

工具按"块间有没有明暗结构"来判：正常照片块间方差几千，灰图只有个位数。

```
正常场景   -> 块内方差21  块间方差2862  -> 正常
灰噪声     -> 块内方差353 块间方差7     -> 灰图!!
```

板子那边对应有两条日志，看到任意一条就说明拍照这条路是通的：

```
[up] 拍照走专用通道 chn1（打包 RGB888 640x480）
[up] 拍照转换 OK：HWC字节拷贝（抽样 9/9 与源帧一致，源帧 {...}）
```

两条都没有，就说明两条路都没走通，把前面几行（带"拍照通道停用/转换失败"的）
发出来。真机上内存不够的话，把 `k230_trigger.py` 里的 `SNAP_CHANNEL` 改成 `False`，
会退回带像素校验的帧转换路径。

固件自检里，有几项是直接在真板子上很难复现的：

```
[ ok ] 短毛刺（<150ms）不能触发整轮          防信号线干扰
[ ok ] 上电后 5 秒内不响应                    信号线还没稳
[ ok ] 电平不回落时不再触发                    靠 armed 位，不会反复开饭
[ ok ] 盆里有粮时跳过那 10s 出粮              传感器分支正确
[ ok ] 盆里没粮时先出 10s 粮                  两条分支时长差约 11s
[ ok ] 到位立刻断电（IN1~IN4 全低）           防 60s 堵转发热
[ ok ] 两轮之间至少要间隔 MIN_GAP             防止短时间连发
```

---

## 11. 常见问题

### 上传报 `programmer is not responding` / `not in sync: resp=0x00`

这**不是**硬件坏，也不是代码问题 —— 是 IDE 里选的 bootloader 类型和板子上实际烧的不一致。
`resp=0x00` + 反复 `attempt N of 10` 的意思是：串口打开了，但发出的握手没人应答。

Arduino Nano 有两种 bootloader，速度不同，选错了就必然报这个：

| 板子上是什么 | IDE 里选 | 波特率 |
|---|---|---|
| **CH340 克隆板（绝大多数）** | `处理器 → ATmega328P (Old Bootloader)` | 57600 |
| 官方板 / 新 bootloader | `处理器 → ATmega328P` | 115200 |

**怎么确定自己是哪种**（不用猜，一条命令就问出来了）。把下面路径里的 avrdude 换成你机器上的版本号：

```powershell
$avr  = "$env:LOCALAPPDATA\Arduino15\packages\arduino\tools\avrdude\8.0.0-arduino1\bin\avrdude.exe"
$conf = "$env:LOCALAPPDATA\Arduino15\packages\arduino\tools\avrdude\8.0.0-arduino1\etc\avrdude.conf"

# -n = 只读签名，不写入，绝对安全
& $avr -C $conf -c arduino -p atmega328p -P COM6 -b 57600  -n -v
& $avr -C $conf -c arduino -p atmega328p -P COM6 -b 115200 -n -v
```

哪个波特率能秒过（约 4 秒），就选哪个。能过的那个会打印：

```
Setting baud rate     : 57600
FW Version            : 1.16                  ← 1.16 = 旧 ATmegaBOOT
Device signature = 1E 95 0F (ATmega328P)      ← 芯片确实是 328P，板子没坏
```

`FW Version` 是 **1.16** 或 **1.15** → 选 Old Bootloader；
是 **4.x / 8.x** → optiboot，选普通 `ATmega328P`。

> 本项目实测的一块 CH340 Nano：57600 秒过、115200 跑满 58 秒全超时，
> `FW Version 1.16` —— 就是需要选 **Old Bootloader**。

排查顺序（前两条能解决 90%）：

1. **`工具 → 处理器 → ATmega328P (Old Bootloader)`** ← 最可能就是这个
2. `工具 → 端口` 选对（别选成蓝牙串口）
3. **关掉串口监视器**（它占着 COM 口，上传会抢不到）
4. 换根 USB 线 —— 便宜线是"只充电不传数据"的，插上能供电所以迷惑性强
5. 上传时手动按一下板子上的 RST（bootloader 只等约 1 秒）

### K230 刷 `sensor(0) snapshot chn(2) failed(3)`

这句不是 Python 报错，是相机底层在报：**chn2（给模型的那路）出不了帧**。
`failed(3)` 在勘智官方社区的解释是 **VB 内存 / 通道配置不足**，不是摄像头坏了。

`PipeLine` 会开两个相机通道，这个错专指 chn2：

```
chn0 -> 屏幕      （DISPLAY_SIZE）
chn2 -> 模型输入   （RGB888P_SIZE）  ← 报错说的是这一路
```

**先说结论：如果你照旧版代码填了 `1280x720` + `800x480`，那就是错的**，
亚博这块屏根本不是 800x480，而且 1280×720 的 RGBP888 一路要 2.7MB，
两块缓冲一占，VB 池就不够了。正确值见下面「分辨率该填多少」。

### 分辨率该填多少（已按亚博实机参数核对）

亚博 K230 的标配是 **GC2093 摄像头（1920×1080）+ 2.4 寸 MIPI 屏（640×480）**，
亚博官方例程里用的也是 640×480。所以：

```python
RGB888P_SIZE = (640, 480)
DISPLAY_SIZE = (640, 480)
DISPLAY_MODE = "lcd"        # 亚博的屏是 ST7701
```

三个约束，缺一个就会出问题：

| 约束 | 说明 |
|---|---|
| `DISPLAY_SIZE` = 屏的真实分辨率 | 填大了花屏/右键，填小了有黑边。2.4 寸是 **640×480**，3.5 寸才是 800×480 |
| `RGB888P_SIZE` 宽度 16 对齐 | sensor 会自动对齐，对齐后实际尺寸和你写的不一样 → 检测框错位 |
| 两者**同比例** | 屏是 4:3 就给 4:3 的帧。给 16:9 会把画面压扁，框和触发区一起算偏 |

> 模型输入只有 320×320，所以 AI 帧用 640×480 一点不亏 —— 反而比 1280×720 省三分之二内存。
> 要 4:3 的高清可以试 `(1280, 960)`，但那要内存够。

**还有两个坑，`PipeLine` 的文档里写得很明白但很容易忽略：**

1. **`create()` 之后 `display_size` 会被屏驱动覆盖。**
   你在构造时传 640×480，实际可能是别的值 → 检测框整体偏位。
   必须 `create()` 之后调 `pl.get_display_size()` 取真实值再算缩放（代码里已经这么做了）。
2. **`get_frame()` 返回的是 `ulab.numpy.ndarray`，不是 image 对象。**
   官方文档写得很清楚：返回 **CHW 三维数据，shape `[3,H,W]`**（不是四维
   `[1,3,H,W]`），而且是**当前帧的引用，不是拷贝** —— 必须在下一次
   `get_frame()` 之前用完。这是正常设计，不是固件版本问题。

   由此带来两个必须分开处理的点：

   | 需求 | 正确做法 |
   |---|---|
   | 画框 / 写字 | 画在 `pl.osd_img`（ARGB8888 图层）上，再 `pl.show_image()` |
   | 拍照 / 压 JPEG | 先转成 `image.Image`：`image.Image(w, h, image.RGBP888, alloc=image.ALLOC_REF, data=frame)` |

   如果图省事把 ndarray 当 image 用（`.draw_rectangle()` / `.compress()`），
   异常会被 try/except 吞掉，表现为"能识别、能放饭，但屏幕上永远没框、
   网页永远没照片"，而且一行错误信息都没有。本项目的 `_frame_to_image()`
   就是干这个转换的；转不出来会明确打日志说"拍照不可用"，不静默。

**按顺序排障，每改一项都要先断电重上电**：

| 顺序 | 做什么 | 为什么 |
|---|---|---|
| 1 | **断电重上电** | CanMV IDE 的 interrupt 不释放相机资源。你日志里正好有 `IDE interrupt`，重跑很容易卡在这 —— 这条最常见也最容易被忽略 |
| 2 | 确认 `DISPLAY_SIZE` 是 **640×480** | 亚博 2.4 寸屏就是这个值。填 800×480 是错的最常见来源 |
| 3 | `RGB888P_SIZE` 改成 `(640, 480)` | 同比例 + 省内存，直接解决 `failed(3)` |
| 4 | `DISPLAY_MODE = "virt"` | 再排除屏幕。不点屏但照样有画面数据 |
| 5 | 摄像头排线插到底、卡扣扣好 | FPC 虚接的典型表现就是"能 init 但没帧" |

不用一个个手改，跑探测器逐个组合试：

```bash
# 在 CanMV IDE 里，把 tools/camera_probe.py 存到 /sdcard/ 后：
exec(open('/sdcard/camera_probe.py').read())
camera_probe.show_list()      # 看有哪些组合
camera_probe.run(2)           # 试第 2 组，然后断电重上电再试下一组
```

它会在 3 秒内数拿到多少帧，哪组能用会直接告诉你该填哪三行参数。
第 0 组就是亚博 K230 的正确配置，先试它。

> 如果**所有组合都是 0 帧**，那就不是配置问题了 ——
> 先用 CanMV 官方的 camera 例程确认相机本身能出图，再回来看这里。
> 若连 `create()` 都过不去，多半是固件不是亚博出厂版
> （`CanMV_K230_YAHBOOM_micropython_V1.3.x`）—— 跨厂固件的引脚和屏驱动不一样。

**Q: 触发线接了，但执行板一点反应都没有**
A: ① 有没有共地（K230 GND ↔ Nano GND）；② 万用表量 D2，触发时有没有变成 3.3V；
③ `TRIG_PIN` 是不是你实际接线的那根（亚博只有 42/43/34/35/33/32/26 这 7 个）；
④ 是不是上电 5 秒内（`BOOT_GUARD_MS`）；⑤ 板子有没有烧 `simple_trigger.ino`。

**Q: 步进电机嗡嗡响但不转 / 反转**
A: 嗡嗡不转 = 丢步，把 `STEP_US` 调大（1500 → 2500）或给步进降压；
反转 = 把 `STEPS_FORWARD` 取负，或对调 A+ 和 A-。
另外确认 L298N 的 ENA/ENB 跳线帽**还装着**（见 2.3 坑 4）。

**Q: 步进电机 / L298N 很烫**
A: 看 2.3。80% 是"12V 直供 L298N 没限流"。先降步进电压到 6~9V，
确认 `HOLD_DEENERGIZE = true`（保持 60 秒期间不通电）。

**Q: 60 秒保持的时候动物早走了，盆还摊在外面**
A: 现在是固定 60 秒（按需求）。想在 K230 侧提前收，就得恢复一条下行通道
   （接 `PIN_BUSY` 或干脆改回串口），或者把 `FEED_HOLD_MS` 改小。

**Q: 盆里有粮，但它还是先出了 10 秒粮**
A: 阈值没设对。看 Arduino 串口打的那行 `#dist=xx.x cm`：
把 `FOOD_PRESENT_CM` 调到"比这个读数大一点"就行（比如读到 15cm，就设 16）。
反过来，空盆也被判成有粮，就把阈值调小。

**Q: 超声波一直报 `-- cm (no echo)`，什么都不动**
A: 这是设计好的"整轮放弃"（不会在测不准时瞎开门）。按顺序查：
① VCC 接 5V 了吗、GND 共地了吗；② TRIG/ECHO 是不是接反了（D11=TRIG, D12=ECHO）；
③ 盆底离探头是不是不到 2cm（盲区）；④ 探头是不是照到盆沿/支架了。
如果只是暂时没好、想让它先能出粮，把 `US_FAIL_FALLBACK_AFTER` 改成 3。

**Q: 超声波读数跳得厉害**
A: 固件已经取 5 次中位数了，还跳就是物理原因：探头松动、粮面不平、盆壁反射。
先把探头固定牢、垂直朝下；盆壁太光滑可以在对面贴点胶带打散回波。

**Q: 有粮和空盆的读数差不多（差不到 3cm）**
A: 盆太浅或探头太远。超声波锥角约 15°，盆浅的时候回波容易被盆底和粮面混在一起。
换个深一点的盆，或让探头离粮面近一些（但不能进 2cm 盲区）。

**Q: `Kmodel file not exist.` / 模型加载失败**
A: **先说结论：出厂卡上本来就有模型，位置是 `/sdcard/kmodel/yolov8n_320.kmodel`**
   （`kmodel` 目录，不是 `models`）。早期版本默认写 `/sdcard/models/`，
   那个目录在出厂卡上不存在，于是必然报这个错。现在的默认配置已经改对了，
   而且加载失败时会**自动**改用卡上真实存在的那个，不必再手改路径。

   如果换了自己训的模型还是报错，按顺序查：

   ① 跑 **`tools/list_models.py`**（CanMV IDE 里直接运行）—— 它会扫 `/sdcard`
      把每个 `.kmodel` 的**真实路径和大小**列出来，并检查配置的路径存不存在。
      加载报错时程序也会自动打印这份清单：
      ```
      !! 卡上实际存在的 .kmodel（2 个）:
      !!   /sdcard/kmodel/yolov8n_320.kmodel        (3.5 MB)
      !!   /sdcard/kmodel/catdog_320.kmodel          (3.1 MB)
      !! -> 把 KMODEL_PATH 改成上面真正存在的那个路径
      ```
   ② 一个 `.kmodel` 都没有 → 模型确实没拷进去。亚博出厂固件是带模型的；
      自己训的按 `train/README.md` 转好后放 `/sdcard/kmodel/`。
   ③ 文件在、加载还报错 → 那是 kmodel 版本和固件不匹配（nncase 版本对不上），
      不是路径问题。

   > 文件名里的 `320` 就是模型输入尺寸，必须和 `MODEL_INPUT_SIZE` 一致。
   > 拿 640 的模型配 320 的配置，表现为**框全错位或完全检不到**，
   > 而不是报错 —— 这种更难查，所以本项目的默认模型名里特意带上了尺寸。
   >
   > 卡上还有个 `yolov8n_224.kmodel`（224 输入），**别拿错**，尺寸对不上。

**Q: 识别不到猫狗 / 屏幕上没有框**
A: 先看串口有没有这一行：
   `[det] run() 返回 N 个张量，首个 shape=...`
   · **没有这行** → 根本没走到推理，看开头 `[det] 模型加载完成` 有没有
   · **有这行但还是框=0** → 张量没解成框。看有没有
     `[det] !! postprocess 失败` 或 `近 5 秒检测到:` 那行
   · **`近 5 秒检测到: person(0.6)` 之类** → 模型工作正常，只是没猫没狗，
     调镜头角度或看 `/sdcard/debug_view.jpg` 到底拍到了什么
   再细查就跑 `tools/det_probe.py`（逐框标注为什么不算数）和
   `tools/det_selftest.py`（加载/推理/后处理分段计时）。
   · 一个框都没有 → 模型没加载
   · 有框但全是 person/car → `LABELS` 和 kmodel 的类别顺序对不上
   · 有 cat/dog 但分数只有 0.2~0.4 → 把 `PRESENCE_ON_CONF` 降到 0.30，或改善光线
   · 分数够高但不触发 → `TRIGGER_ZONE` 设错了（框的**中心**要在区里）
   详见 README **5.1 识别不到猫狗怎么查**。

**Q: 存图报 `current format not support save function!`**
A: 说明那一帧是**平面格式**（RGBP888），`save()` 只认打包格式（RGB565/RGB888）。
   现在改成 `compress()` 取 JPEG 字节再自己写文件，平面格式也能存；
   万一还不行，会退到 `sensor.snapshot()` 直拍（RGB565，一定能存）。
   看到 `[beat] 已存 ... via compress(...)` 或 `via sensor 直拍` 就是成功了。

**Q: 想让它一直高电平 / 想让上传更频繁**
A: 电平语义本来就是"有动物就一直高"（`PRESENCE_OFF_MS` 决定多久算走了）。
   上传频率改 `UPLOAD_INTERVAL_MS`（默认 60000 = 1 分钟）。
   别调到 5 秒以内 —— 传一张要等网络，会把帧率拖到连识别都跟不上。

**Q: 猫蹲着不走，会一直放着不管吗 / 补粮节奏在哪调**
A: 会定时补。执行板在**电平一直高**的情况下每 2 分钟再跑一轮
   （`RE_FEED_WHILE_HIGH_MS = 120000`）。计时起点是**上一轮的开始时刻**，
   所以是字面意义上的"2 分钟一个周期"（一轮本身约 63~74 秒，剩下的是空档）。
   动物走了（电平变低）立刻停止补放。
   想改节奏就改这个值；但**别小于一轮的时长**（约 74 秒），否则机器没有喘息时间。
   注意：补粮的判断完全在执行板上，K230 不参与 —— 所以 K230 死机、
   WiFi 断了，只要线还高着，照样按周期补。
   另外补粮**不会**增加网页上的"到访次数"（到访仍按电平的上升沿算）。

**Q: K230 和 PC 不在同一局域网 / 想放云端怎么办**
A: 可以，不用改架构 —— K230 是主动连出去的一方，家里**不用做端口映射**。
   把 `SERVER_URL` 改成公网地址就行，但**三件事必须做**：
   ① 设 `API_TOKEN`（和服务端 `CATDOG_TOKEN` 一致，否则 403）；
   ② 用 `https://`（公网明文 HTTP 等于公开照片）；
   ③ `UPLOAD_TIMEOUT_S` 调到 8 秒以上，否则窄带上行会大量超时。
   详见 **6.5 放到云端**。

**Q: 上传报 403**
A: token 不匹配。K230 的 `API_TOKEN` 和服务端的 `CATDOG_TOKEN` 必须是同一个值，
   注意别带空格/引号。K230 上 token 只能含字母数字和 `- _ .`。

**Q: 网页打不开**
A: ① `python pc/run.py` 还在跑吗；② 防火墙放行 8000；
③ 手机/别的电脑访问要用**局域网 IP**，不是 `127.0.0.1`。

**Q: 屏幕上没有框 / 网页上一直没有照片，但放饭正常**
A: 这几乎一定是同一个原因：把 `get_frame()` 返回的 ndarray 当成 image 对象用了。
   官方文档明确 `get_frame()` 返回 **CHW ndarray `[3,H,W]`**，上面没有
   `.draw_rectangle()` / `.compress()`，异常会被 try/except 吞掉，所以一声不响。

   本项目的处理（不用你改）：
   - 画框 → 画在 `pl.osd_img` 图层上
   - 拍照 → `_frame_to_image()` 转成 `image.Image` 再裁剪压缩

   如果串口出现 `!! 这一帧转不成 image.Image`，说明这个固件的 `image` 模块
   不支持 `ALLOC_REF`，把那段日志发出来即可 —— **检测和放饭完全不受影响**，
   只是暂时没有照片。

**Q: K230 上传失败，但放饭正常**
A: 这是设计好的：上传是尽力而为，失败进 `/sdcard/captures`，
   联网后每 20 秒补传 2 张。看 `[up]` 开头的日志确认失败原因
   （多半是地址填错 / token 不匹配 / 防火墙）。

**Q: 每次来都识别成新个体（个体数一直涨）**
A: `MATCH_THRESHOLD` 太高。打开网页看"相似度"列：如果同一只的相似度在 0.85 左右，
就把阈值降到 0.80 试试。反过来，如果两只不同的猫被合成一只，就调高。

**Q: 同一只猫被算成好几次到访**
A: 网页上的"到访次数"由 K230 判定的（电平从低变高算一次）。
   如果同一只猫被算多次，说明电平在抖 —— 也就是检测在漏帧。按顺序查：
   ① `TRIGGER_ZONE` 太大，远处路过的也进来了 → 缩到饭盆周围那一块；
   ② 光线差/目标太远，置信度在门槛附近来回跳 → 把 `CONF_THRESH` 或
      `PRESENCE_OFF_MS` 调大一点；
   ③ 用 `tools/det_probe.py` 看是不是大量 `[LOW]`（分数不够的候选）。
   注意这跟"补粮"是两件事：补粮由执行板按 2 分钟周期做，**不会**增加网页上的到访次数。

**Q: K230 死机了，门一直开着**
A: 不会。执行板这一轮是本地跑完的，跟 K230 在不在没关系：
   60 秒到了就退回、关门、断电（`IN1~IN4` 全低），跑完打印 `#=== feed cycle done ===`。

**Q: 插着 USB 会影响触发吗**
A: 不会。触发走 D2，USB 用的是 D0/D1，两者不冲突。所以可以边跑边看调试输出。

**Q: CanMV IDE 右边预览是黑的，但板载屏有画面**
A: 正常。`DISPLAY_MODE = "lcd"` 时画面只送到板载屏，IDE 的帧缓冲区不会收到数据。
   想在 IDE 里看画面就改成 `DISPLAY_MODE = "virt"`（此时板载屏不显示）。
   另外，**IDE 的帧缓冲区预览最好在 run 之前关掉** —— 它和 LCD 驱动抢显示层，
   是 `PipeLine.create()` 卡住的常见原因之一。

**Q: 日志停在 `vb common pool count 6` 不动了（看起来像死机）**
A: 卡在 `PipeLine.create()` 里，属于 C 层的阻塞，Python 不会抛异常。
   这里有个**关键陷阱**：卡死时 `try/except` 抓不住，所以"失败就换模式"的
   循环**根本没机会执行** —— 光靠重试救不回来。现在的代码专门对付这一点：

   | 机制 | 对付什么 |
   |---|---|
   | 开跑前把模式写进 `/sdcard/.k230_boot` | 卡死重启后知道"上次撞在 lcd 上"，直接跳过 |
   | 看门狗线程（45 秒） | 卡住就自动 `machine.reset()`，**不用你去拔线** |
   | 启动成功就删记录文件 | 下次恢复首选模式，不会一直降级 |
   | 启动前 `sensor.stop()` / `display.deinit()` | 清掉上一次运行残留的缓冲池 |

   也就是说：**第一次卡住 → 45 秒后自动重启 → 这次用 virt → 一定能起来。**
   起来之后日志里会明确写：

   ```
   [main] 上次启动卡在 mode=lcd 的 create() 里（累计 1 次）
   [main] 这次先从 mode=virt 开始
   ```

   想手动控制就把 `ENABLE_BOOT_WATCHDOG = False`（那就还是要拔线）。

   如果连重启后也起不来，按这个顺序查：

   | 步骤 | 原因 |
   |---|---|
   | ① IDE 里先按**停止**，等 3 秒再点运行 | 上一次的 sensor/显示层没释放 |
   | ② 拔 USB 重新插（冷启动） | 上电最干净，最能清残留状态 |
   | ③ 关掉 IDE 的帧缓冲区预览 | 和 LCD 抢显示层（这是最常见的元凶） |
   | ④ 屏排线重新插紧；不接屏就 `DISPLAY_MODE="virt"` | 屏初始化阻塞 |
   | ⑤ 删掉 `/sdcard/.k230_boot` | 记录乱了，重新从首选模式开始 |

   现在 `create()` 每一步都有日志，能看出卡在哪个阶段：

   ```
   [main] 初始化 PipeLine   mode=lcd  AI帧 640x480  屏幕 640x480
   [main]   对象已创建，开始 create()（卡住就会自动重启）...
   ```

   如果 `lcd` 抛异常，会自动按 `virt -> auto` 再试，并把失败实例 `destroy()` 掉，
   避免残留状态把下一轮也带崩。

---

## 12. 要烧/要跑的就两个文件

```
K230 排针第 1 脚 (GPIO42, 3.3V) ────> Arduino D2     电平高 = 有动物
Arduino 收到高电平 -> 自己跑完整一轮（测粮 / 出粮 / 开门 / 步进 / 保持 60s / 收回 / 关门）
```

| 文件 | 说明 |
|---|---|
| `k230_trigger.py` | ★ K230 单文件，CanMV IDE 里直接运行 |
| `arduino/simple_trigger/simple_trigger.ino` | ★ Arduino 单文件，烧进 **Arduino Nano** |
| `tools/gpio_probe.py` | 不确定引脚时先跑这个，确认 GPIO 真的能输出 |
| `tools/list_models.py` | 报 `Kmodel file not exist` 时先跑这个，把卡上真实路径列出来 |
| `tools/camera_probe.py` | 报 `snapshot chn(2) failed` 时逐个分辨率/显示模式试 |
| `tools/yolo_inspect.py` | 永远 0 个框 / postprocess 卡住时，打印库源码和真实返回值 |
| `tools/sim_k230_upload.py` | 上传报 ECONNRESET 时，在 PC 上用同样的写法验证服务端（默认拒绝打生产库） |
| `tools/prune_visits.py` | **网页数据对不上串口**时用：清测试数据、合并重复照片、删 0 次到访的个体 || `tools/run_official_example.py` | 一条命令在板子上跑官方例程，分清"硬件问题"还是"逻辑问题" |
| `tools/check_photos.py` | **网页上是灰图 / 看不出画面**时用：逐张判"正常还是灰图" |

> 启动时也会自动打印 `libs/YOLO.py` 的 `run`/`postprocess`/`draw_result`
> 和 `libs/PipeLine.py` 的 `create`/`get_frame` 源码（只读文件，不会卡）。
> "为什么永远 0 个框""为什么 chn2 不出帧"这两个问题的答案都在里面。
| `arduino/simple_trigger/README.md` | 执行板专属接线/参数/上板顺序 |

### 引脚别猜（已联网核对）

`TRIG_PIN` 填的是 **GPIO 编号**（`Pin(42)` = GPIO42），不是排针序号。

亚博 YAHBOOM K230 只引出 7 个 GPIO，全在散热片旁 12pin 排针上：

| 排针脚 | GPIO | 可复用功能 | 能不能当触发脚 |
|---|---|---|---|
| 1 | GPIO42 | GPIO42 / UART1_RTS / PWM0 / QSPI1_D2 | ✅ **默认就是普通 GPIO，首选** |
| 2 | GPIO43 | GPIO43 / UART1_CTS / PWM1 / QSPI1_D3 | ✅ 次选 |
| 8 | GPIO34 | IIC1_SCL / IIS_D_IN0 / PDM_IN3 / UART3_RTS | ⚠️ 用音频就没了 |
| 10 | GPIO35 | IIC1_SDA / IIS_D_OUT0 / PDM_IN1 / UART3_CTS | ⚠️ 同上 |
| 3 | GPIO33 | IIC0_SDA / IIS_WS / UART3_RXD | ⚠️ 同上 |
| 5 | GPIO32 | IIC0_SCL / IIS_CLK / UART3_TXD | ⚠️ 同上 |
| 6 | GPIO26 | **MMC1_CLK** / RESV / PDM_CLK | ❌ **SDIO 时钟，用了 WiFi 就别碰** |

外加 USB 口旁边那个 4pin 通讯口：`5V / GND / GPIO9(UART1_TXD) / GPIO10(UART1_RXD)`。
本项目**不用它**（不需要串口），但如果你以后想加下行通道，可以拿它当 UART1 用。

所以默认值就是 `TRIG_PIN = 42`。想再确认一遍就跑：

```python
# tools/gpio_probe.py
CANDIDATES = [42, 43, 34, 35, 33, 32, 26]
```

每个脚闪 6 秒，串口打印"现在轮到 GPIO42（排针第 1 脚）"。拿 LED+1k 电阻
（或万用表）量排针，哪根闪就填哪根。**轮到某个脚时板子重启/卡住 → 跳过它。**

> 你原来给的候选里那个 `3` 在亚博板上没有引出（GPIO3 = JTAG_TDI/UART1_TXD，
> 只存在于芯片封装上）。你的 7 个脚实际是 `32 / 33 / 42 / 43 / 26 / 34 / 35`。
> **GPIO26 是 SDIO 时钟**，你用 WiFi 上传，所以它不能当触发脚。

### 想加密反馈线

默认是开环的（K230 发完就不管了）。如果希望 K230 知道执行板什么时候跑完：

```
Arduino D6 (5V) ──[1k]──┬──> K230 排针第 2 脚 (GPIO43)
                        │
                      [2k] → GND
```

固件 `PIN_BUSY` 改成 `6`，`k230_trigger.py` 的 `BUSY_PIN` 改成 `43`。
**必须 1k/2k 分压**（Arduino 是 5V，K230 输入只吃 3.3V）。

自检（不用硬件）：

```bash
python tests/k230_trigger_sim.py        # 75 项：电平消抖、一只动物只喂一次、类别映射、URL/token、补传
tests/arduino/simple_trigger_test.exe   # 42 项：有粮/没粮分支、超声测距、毛刺过滤、MIN_GAP
powershell -File tests/arduino/build_nano.ps1   # 按 ATmega328P 真编译，报 flash/RAM
```
