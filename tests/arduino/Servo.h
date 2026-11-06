#pragma once
/* Arduino Servo 库的最小桩，只保留 .ino 用到的接口 */

#include <cstdint>

class Servo {
public:
  void attach(uint8_t pin) { pin_ = pin; attached_ = true; }
  void detach() { attached_ = false; }
  void write(int a) {
    if (a < 0) a = 0;
    if (a > 180) a = 180;
    angle_ = a;
    writes_++;
  }

  bool attached() const { return attached_; }
  int angle() const { return angle_; }
  uint8_t pin() const { return pin_; }
  int writes() const { return writes_; }

private:
  uint8_t pin_ = 0;
  bool attached_ = false;
  int angle_ = 90;
  int writes_ = 0;
};
