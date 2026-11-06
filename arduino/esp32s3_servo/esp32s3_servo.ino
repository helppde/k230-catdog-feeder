/*
 * ============================================================
 *  ESP32-S3 舵机串口控制（Arduino IDE）
 * ============================================================
 *
 * 功能：
 *   - 上电后舵机停在 INIT_ANGLE（默认 90 度）
 *   - 串口按行输入角度（0~180），舵机平滑转过去
 *
 * --------------------------- 命令表 ---------------------------
 *   90            转到 90 度（只写数字即可，0~180，超出自动夹紧）
 *   C             回中（INIT_ANGLE）
 *   S             往返扫动演示（0 <-> 180），再发一次 S 停止
 *   D             detach，松开舵机（不再输出脉冲，不会抖、不发热）
 *   A             重新 attach，恢复输出（会自动回到当前角度）
 *   ?             查询当前角度
 *
 * 每个回车算一条命令，回显格式：
 *   OK <目标角度>  执行成功
 *   ERR <原因>     命令非法
 *
 * ---------------------- 关于串口输出（重要） ----------------------
 * ESP32-S3 上 "Serial" 到底连哪个硬件，取决于 Tools -> USB CDC On Boot：
 *
 *   USB CDC On Boot = Disabled  ->  Serial = UART0（CH340/CP2102 那个口）
 *   USB CDC On Boot = Enabled   ->  Serial = 原生 USB（HWCDC/TinyUSB）
 *
 * 这是 S3 上最常见的"烧进去了但串口监视器一片空白"的原因：口设成
 * Enabled 之后，Serial.print 全跑到原生 USB 上去了，CH340 上什么都没有。
 *
 * 本文件用 CONSOLE 统一输出：UART0(Serial0) 永远有输出；如果 CDC 也被
 * 启用，再同时往原生 USB 镜像一份。这样两种设置下都能看到。
 * 输入同样两个口都收。
 *
 * --------------------------- 接线 ---------------------------
 *   舵机信号线(橙/黄) -> GPIO4
 *   舵机电源 红线     -> 外部 5V 电源正极（不要用板子 5V/USB 直接带大舵机）
 *   舵机地线 棕/黑    -> 外部电源负极，并且和 ESP32-S3 的 GND 连在一起（共地！）
 *
 * ------------------------ Arduino IDE 设置 ------------------------
 *   开发板：ESP32S3 Dev Module
 *   库    ：库管理器搜索安装 "ESP32Servo"
 *   串口监视器波特率 115200，行结束符 "Newline"
 * ============================================================
 */

#include <ESP32Servo.h>

/* ==================== 1. 可调参数 ==================== */
const uint8_t PIN_SERVO = 4;          /* 舵机信号脚，按实际接线改 */

const int INIT_ANGLE  = 90;           /* 上电初始角度 */
const int ANGLE_MIN   = 0;            /* 机械行程下限，装上机构后建议收紧 */
const int ANGLE_MAX   = 180;          /* 机械行程上限，防止顶死堵转 */

/* 脉宽标定：SG90/MG90S 常见 500~2400us；若 0 度和 180 度到不了位，
 * 就微调这两个值（比如 600 / 2400）。 */
const int PULSE_MIN_US = 500;
const int PULSE_MAX_US = 2400;

const int MOVE_SPEED_DPS = 240;       /* 转动速度：度/秒，0 = 立即到位 */
const int UPDATE_MS      = 20;        /* 平滑刷新周期 */

const unsigned long SERIAL_BAUD = 115200;

/* ==================== 2. 串口双路输出 ==================== */
/* ARDUINO_USB_CDC_ON_BOOT = 1 时 Serial 是原生 USB；此时额外镜像一份。
 * = 0 时 Serial 就是 Serial0，不需要重复打印。 */
#if defined(ARDUINO_USB_CDC_ON_BOOT) && (ARDUINO_USB_CDC_ON_BOOT)
  #define CONSOLE_MIRROR_USB 1
#else
  #define CONSOLE_MIRROR_USB 0
#endif

void consoleBegin() {
  Serial0.begin(SERIAL_BAUD);          /* UART0：CH340 那个口，永远可用 */
#if CONSOLE_MIRROR_USB
  Serial.begin(SERIAL_BAUD);           /* 原生 USB CDC，没插线也不影响运行 */
#endif
}

template <typename T> void conPrint(const T &v) {
  Serial0.print(v);
#if CONSOLE_MIRROR_USB
  Serial.print(v);
#endif
}

void conPrintln() {
  Serial0.println();
#if CONSOLE_MIRROR_USB
  Serial.println();
#endif
}

template <typename T> void conPrintln(const T &v) {
  Serial0.println(v);
#if CONSOLE_MIRROR_USB
  Serial.println(v);
#endif
}

/* 两个口都可能有字节进来 */
bool consoleAvailable() {
  if (Serial0.available() > 0) return true;
#if CONSOLE_MIRROR_USB
  if (Serial.available() > 0) return true;
#endif
  return false;
}

int consoleRead() {
  if (Serial0.available() > 0) return Serial0.read();
#if CONSOLE_MIRROR_USB
  if (Serial.available() > 0) return Serial.read();
#endif
  return -1;
}

/* ==================== 3. 全局状态 ==================== */
Servo servo;

int  curAngle = INIT_ANGLE;           /* 当前已输出的角度 */
int  tgtAngle = INIT_ANGLE;           /* 目标角度 */
bool attached = false;                /* 舵机是否处于 attach 状态 */

unsigned long lastStepMs = 0;

bool sweepMode = false;               /* 扫动演示进行中 */
bool sweepUp   = true;
unsigned long lastSweepMs = 0;

/* ==================== 4. 舵机基本操作 ==================== */

void attachServo() {
  if (attached) return;
  servo.attach(PIN_SERVO, PULSE_MIN_US, PULSE_MAX_US);
  servo.write(curAngle);              /* 挂上瞬间先给当前角度，避免乱甩 */
  attached = true;
}

void detachServo() {
  if (!attached) return;
  servo.detach();
  attached = false;
}

int clampAngle(long a) {
  if (a < ANGLE_MIN) a = ANGLE_MIN;
  if (a > ANGLE_MAX) a = ANGLE_MAX;
  return (int)a;
}

void setTarget(int a) {
  tgtAngle = clampAngle(a);
  attachServo();
  lastStepMs = millis();
}

/* 非阻塞平滑：每 UPDATE_MS 把 curAngle 往 tgtAngle 推一点 */
void updateMotion() {
  if (!attached) return;
  if (curAngle == tgtAngle) return;

  unsigned long now = millis();
  unsigned long dt  = now - lastStepMs;
  if (dt < (unsigned long)UPDATE_MS) return;
  lastStepMs = now;

  int step;
  if (MOVE_SPEED_DPS <= 0) {
    step = 180;                       /* 立即到位 */
  } else {
    step = (int)((long)MOVE_SPEED_DPS * (long)dt / 1000);
    if (step < 1) step = 1;
  }

  if (curAngle < tgtAngle) {
    curAngle += step;
    if (curAngle > tgtAngle) curAngle = tgtAngle;
  } else {
    curAngle -= step;
    if (curAngle < tgtAngle) curAngle = tgtAngle;
  }
  servo.write(curAngle);
}

/* 扫动演示：到位后自动换方向 */
void updateSweep() {
  if (!sweepMode) return;
  if (curAngle != tgtAngle) return;    /* 还在路上 */

  unsigned long now = millis();
  if (now - lastSweepMs < 400) return; /* 端点停一下，别来回抽搐 */

  lastSweepMs = now;
  if (curAngle >= ANGLE_MAX) sweepUp = false;
  if (curAngle <= ANGLE_MIN) sweepUp = true;
  setTarget(sweepUp ? ANGLE_MAX : ANGLE_MIN);
}

/* ==================== 5. 串口命令 ==================== */

char    lineBuf[24];
uint8_t lineLen = 0;

/* 回复的是"目标角度"，不是当前角度 —— 因为是平滑移动，
 * 回复那一刻舵机还在半路上，报 curAngle 会让人以为没收到命令。 */
void replyOK() {
  conPrint("OK ");
  conPrintln(tgtAngle);
}

void replyErr(const char *why) {
  conPrint("ERR ");
  conPrintln(why);
}

/* 判断整行是不是纯数字（允许前面一个负号） */
bool isNumber(const char *s) {
  if (*s == '-' || *s == '+') s++;
  if (*s == '\0') return false;
  while (*s) {
    if (*s < '0' || *s > '9') return false;
    s++;
  }
  return true;
}

void execLine(char *s) {
  /* 去掉尾部空白 */
  int n = strlen(s);
  while (n > 0 && (s[n - 1] == ' ' || s[n - 1] == '\t')) s[--n] = '\0';

  if (n == 0) return;

  /* 1) 纯数字 -> 直接当角度 */
  if (isNumber(s)) {
    long a = atol(s);
    sweepMode = false;
    if (a < ANGLE_MIN || a > ANGLE_MAX) {
      conPrint("# 超出范围，已夹紧到 ");
      conPrint(clampAngle(a));
      conPrintln(" 度");
    }
    setTarget((int)a);
    replyOK();
    return;
  }

  /* 2) 字母命令（大小写都认） */
  char c = s[0];
  if (c >= 'a' && c <= 'z') c = (char)(c - 'a' + 'A');

  switch (c) {
    case 'C':                          /* 回中 */
      sweepMode = false;
      setTarget(INIT_ANGLE);
      replyOK();
      break;

    case 'S':                          /* 扫动演示 开/关 */
      sweepMode = !sweepMode;
      if (sweepMode) {
        sweepUp = (curAngle < (ANGLE_MIN + ANGLE_MAX) / 2);
        lastSweepMs = 0;
        setTarget(sweepUp ? ANGLE_MAX : ANGLE_MIN);
        conPrintln("# 扫动演示开始，再发 S 停止");
      } else {
        conPrintln("# 扫动演示停止");
      }
      replyOK();
      break;

    case 'D':                          /* 松开舵机 */
      sweepMode = false;
      detachServo();
      conPrintln("# 已 detach，舵机不再出力（手动可以扳动）");
      replyOK();
      break;

    case 'A':                          /* 重新挂上 */
      attachServo();
      conPrintln("# 已 attach");
      replyOK();
      break;

    case '?':                          /* 查询 */
      conPrint("# 当前 ");
      conPrint(curAngle);
      conPrint(" 度，目标 ");
      conPrint(tgtAngle);
      conPrint(" 度，");
      conPrintln(attached ? "已 attach" : "已 detach");
      replyOK();
      break;

    default:
      replyErr("未知命令（输入 0~180 的数字，或 C/S/D/A/?）");
      break;
  }
}

void handleSerial() {
  while (consoleAvailable()) {
    int r = consoleRead();
    if (r < 0) break;
    char c = (char)r;

    if (c == '\n' || c == '\r') {
      if (lineLen > 0) {
        lineBuf[lineLen] = '\0';
        execLine(lineBuf);
        lineLen = 0;
      }
    } else if (c != ' ' && c != '\t') {
      if (lineLen < sizeof(lineBuf) - 1) lineBuf[lineLen++] = c;
      else lineLen = 0;                /* 超长行丢弃，防乱码卡死 */
    }
  }
}

/* ==================== 6. setup / loop ==================== */
void setup() {
  consoleBegin();
  delay(500);                          /* 等 USB CDC 枚举，别把开机提示丢了 */

  servo.setPeriodHertz(50);            /* 50Hz 标准舵机频率，必须 attach 之前设置 */
  attachServo();
  servo.write(INIT_ANGLE);
  curAngle = tgtAngle = INIT_ANGLE;
  delay(300);                          /* 给舵机一点时间走到 90 度 */

  conPrintln();
  conPrintln("=== ESP32-S3 Servo Ready ===");
  conPrint("# 初始角度 ");
  conPrint(INIT_ANGLE);
  conPrint(" 度，行程 ");
  conPrint(ANGLE_MIN);
  conPrint(" ~ ");
  conPrint(ANGLE_MAX);
  conPrintln(" 度");
  conPrintln("# 输入角度(如 45)回车即可；C=回中 S=扫动 D=松开 A=挂上 ?=查询");
}

void loop() {
  handleSerial();
  updateMotion();
  updateSweep();
}
