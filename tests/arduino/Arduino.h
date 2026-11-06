#pragma once
/*
 * 极简 Arduino 桩头文件，只为在 PC 上用 g++ 编译 simple_trigger.ino 做语法/逻辑自检。
 * 不参与板上运行。
 *
 * 为了让测试能"假装"传感器被压住 / 滑台到位，这里的数字和模拟输入
 * 都从全局数组读，测试里直接改数组即可（见 simple_trigger_test.cpp）。
 */
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <cstdio>
#include <string>

#define HIGH 1
#define LOW 0
#define OUTPUT 1
#define INPUT 0
#define INPUT_PULLUP 2

#define A0 14
#define A1 15
#define A2 16

extern std::string g_serialIn;
extern std::string g_serialOut;
extern unsigned long g_millis;     /* 毫秒时钟，delay() 会推进它 */
extern unsigned long g_micros;     /* 微秒时钟，delayMicroseconds() 累加 */
extern int g_ena;                  /* 11 脚电平，测试用来断言"动作结束必须断电" */
extern int g_enb;                  /* 12 脚电平 */
extern int g_pinLevel[24];         /* 数字输入，默认 HIGH（= 开关没压 = 没粮） */
extern int g_analog[24];           /* 模拟输入 */

/* HC-SR04 的 ECHO 用：pulseIn 返回的微秒数（按引脚存）。
 * 0 = 没有回波，pulseIn 会一直等到 timeout 才返回 —— 和真硬件一样。 */
extern int g_echoUs[24];

/* 需要"每次调用返回不同值"时（比如测中位数要造噪声），装这个钩子；
 * 返回 0 表示没回波。不装就读 g_echoUs[pin]。 */
extern unsigned long (*g_pulseInHook)(uint8_t pin, uint8_t state, unsigned long timeout);

/* 测试钩子：每次 delay()/delayMicroseconds() 都会被调用一次。
 * 用它可以在固件"阻塞等待"的过程中注入串口字节（模拟急停/心跳中途到达）。*/
extern void (*g_tickHook)(void);

inline void pinMode(uint8_t, uint8_t) {}

inline int digitalRead(uint8_t pin) {
  return (pin < 24) ? g_pinLevel[pin] : HIGH;
}

inline void digitalWrite(uint8_t pin, uint8_t val) {
  if (pin == 11) g_ena = val;
  if (pin == 12) g_enb = val;
  if (pin < 24) g_pinLevel[pin] = val ? HIGH : LOW;
}

inline void analogWrite(uint8_t pin, int val) {
  if (pin == 11) g_ena = val;
  if (pin == 12) g_enb = val;
}

inline int analogRead(uint8_t pin) {
  return (pin < 24) ? g_analog[pin] : 0;
}

/* HC-SR04：ECHO 高电平的宽度（µs）。
 *
 * 关键行为要跟真硬件一致：**没回波时它会一直阻塞到 timeout 才返回 0**。
 * 这样"测距失败"的用例才能真的验证固件没有卡死/死循环。 */
inline unsigned long pulseIn(uint8_t pin, uint8_t state, unsigned long timeout) {
  unsigned long v;
  if (g_pulseInHook) v = g_pulseInHook(pin, state, timeout);
  else               v = (pin < 24) ? (unsigned long)g_echoUs[pin] : 0;

  unsigned long spent = v ? v : timeout;   /* 没回波就等满超时 */
  g_micros += spent;
  g_millis += spent / 1000;
  if (g_tickHook) g_tickHook();
  return v;
}

inline void delay(unsigned long ms) {
  g_millis += ms;
  if (g_tickHook) g_tickHook();
}

inline void delayMicroseconds(unsigned long us) {
  g_micros += us;
  g_millis += us / 1000;
  if (g_tickHook) g_tickHook();
}

inline unsigned long millis() { return g_millis; }

class SerialStub {
public:
  void begin(unsigned long) {}
  int available() { return (int)g_serialIn.size(); }
  int read() {
    if (g_serialIn.empty()) return -1;
    char c = g_serialIn[0];
    g_serialIn.erase(0, 1);
    return (int)(unsigned char)c;
  }
  void print(char c) { g_serialOut += c; }
  void print(const char *s) { g_serialOut += s; }
  void print(int v) { char b[24]; snprintf(b, sizeof(b), "%d", v); g_serialOut += b; }
  void print(unsigned int v) { char b[24]; snprintf(b, sizeof(b), "%u", v); g_serialOut += b; }
  void print(long v) { char b[24]; snprintf(b, sizeof(b), "%ld", v); g_serialOut += b; }
  void print(unsigned long v) { char b[24]; snprintf(b, sizeof(b), "%lu", v); g_serialOut += b; }
  void println() { g_serialOut += '\n'; }
  void println(char c) { g_serialOut += c; g_serialOut += '\n'; }
  void println(const char *s) { g_serialOut += s; g_serialOut += '\n'; }
  void println(int v) { print(v); println(); }
  void println(unsigned int v) { print(v); println(); }
  void println(long v) { print(v); println(); }
  void println(unsigned long v) { print(v); println(); }
};

extern SerialStub Serial;
