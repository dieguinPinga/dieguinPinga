// Helpers de color RGB565 del original.
#pragma once
#include <Arduino.h>

inline uint16_t lerp565(uint16_t c1, uint16_t c2, float t) {
  if (t < 0) t = 0;
  if (t > 1) t = 1;
  int r1 = (c1 >> 11) & 0x1F;
  int g1 = (c1 >> 5) & 0x3F;
  int b1 = c1 & 0x1F;
  int r2 = (c2 >> 11) & 0x1F;
  int g2 = (c2 >> 5) & 0x3F;
  int b2 = c2 & 0x1F;
  uint8_t r = r1 + (int)((r2 - r1) * t);
  uint8_t g = g1 + (int)((g2 - g1) * t);
  uint8_t b = b1 + (int)((b2 - b1) * t);
  return (r << 11) | (g << 5) | b;
}

inline uint16_t scale565(uint16_t c, float factor) {
  if (factor < 0) factor = 0;
  if (factor > 1) factor = 1;
  uint8_t r = (c >> 11) & 0x1F;
  uint8_t g = (c >> 5) & 0x3F;
  uint8_t b = c & 0x1F;
  r = (uint8_t)(r * factor);
  g = (uint8_t)(g * factor);
  b = (uint8_t)(b * factor);
  return (r << 11) | (g << 5) | b;
}
