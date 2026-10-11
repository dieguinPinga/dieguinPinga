#include "canvas565.h"
#include <esp_heap_caps.h>

bool Canvas565::begin(int w, int h) {
  _w = w;
  _h = h;
  size_t bytes = (size_t)w * (size_t)h * sizeof(lv_color_t);
  _back = (lv_color_t*)heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  _front = (lv_color_t*)heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!_back || !_front) return false;
  memset(_back, 0, bytes);   // negro en RGB565 (con o sin swap)
  memset(_front, 0, bytes);
  _dirtyCount = 0;
  return true;
}

void Canvas565::drawFastHLine(int x, int y, int w, uint16_t c) {
  if (w <= 0 || (unsigned)y >= (unsigned)_h) return;
  if (x < 0) { w += x; x = 0; }
  if (x + w > _w) w = _w - x;
  if (w <= 0) return;
  lv_color_t lc = toLv(c);
  lv_color_t* p = _back + y * _w + x;
  for (int i = 0; i < w; i++) p[i] = lc;
}

void Canvas565::drawFastVLine(int x, int y, int h, uint16_t c) {
  if (h <= 0 || (unsigned)x >= (unsigned)_w) return;
  if (y < 0) { h += y; y = 0; }
  if (y + h > _h) h = _h - y;
  if (h <= 0) return;
  lv_color_t lc = toLv(c);
  lv_color_t* p = _back + y * _w + x;
  for (int i = 0; i < h; i++, p += _w) *p = lc;
}

void Canvas565::fillRect(int x, int y, int w, int h, uint16_t c) {
  if (y < 0) { h += y; y = 0; }
  if (y + h > _h) h = _h - y;
  for (int yy = y; yy < y + h; yy++) drawFastHLine(x, yy, w, c);
}

void Canvas565::drawRect(int x, int y, int w, int h, uint16_t c) {
  drawFastHLine(x, y, w, c);
  drawFastHLine(x, y + h - 1, w, c);
  drawFastVLine(x, y, h, c);
  drawFastVLine(x + w - 1, y, h, c);
}

void Canvas565::drawLine(int x0, int y0, int x1, int y1, uint16_t c) {
  if (y0 == y1) {
    if (x1 < x0) { int t = x0; x0 = x1; x1 = t; }
    drawFastHLine(x0, y0, x1 - x0 + 1, c);
    return;
  }
  if (x0 == x1) {
    if (y1 < y0) { int t = y0; y0 = y1; y1 = t; }
    drawFastVLine(x0, y0, y1 - y0 + 1, c);
    return;
  }
  int dx = abs(x1 - x0), sx = x0 < x1 ? 1 : -1;
  int dy = -abs(y1 - y0), sy = y0 < y1 ? 1 : -1;
  int err = dx + dy;
  for (;;) {
    drawPixel(x0, y0, c);
    if (x0 == x1 && y0 == y1) break;
    int e2 = 2 * err;
    if (e2 >= dy) { err += dy; x0 += sx; }
    if (e2 <= dx) { err += dx; y0 += sy; }
  }
}

void Canvas565::markDirty(int x, int y, int w, int h) {
  int x1 = x < 0 ? 0 : x;
  int y1 = y < 0 ? 0 : y;
  int x2 = x + w - 1;
  int y2 = y + h - 1;
  if (x2 >= _w) x2 = _w - 1;
  if (y2 >= _h) y2 = _h - 1;
  if (x2 < x1 || y2 < y1) return;

  // Fusionar con un rectangulo que se superponga
  for (int i = 0; i < _dirtyCount; i++) {
    Rect& r = _dirty[i];
    if (x1 <= r.x2 + 1 && x2 >= r.x1 - 1 && y1 <= r.y2 + 1 && y2 >= r.y1 - 1) {
      if (x1 < r.x1) r.x1 = x1;
      if (y1 < r.y1) r.y1 = y1;
      if (x2 > r.x2) r.x2 = x2;
      if (y2 > r.y2) r.y2 = y2;
      return;
    }
  }
  if (_dirtyCount < MAX_DIRTY) {
    _dirty[_dirtyCount++] = { x1, y1, x2, y2 };
    return;
  }
  Rect& r = _dirty[MAX_DIRTY - 1];
  if (x1 < r.x1) r.x1 = x1;
  if (y1 < r.y1) r.y1 = y1;
  if (x2 > r.x2) r.x2 = x2;
  if (y2 > r.y2) r.y2 = y2;
}

void Canvas565::publish(lv_obj_t* canvasObj, int screenX, int screenY) {
  for (int i = 0; i < _dirtyCount; i++) {
    const Rect& r = _dirty[i];
    size_t rowBytes = (size_t)(r.x2 - r.x1 + 1) * sizeof(lv_color_t);
    for (int y = r.y1; y <= r.y2; y++) {
      memcpy(_front + y * _w + r.x1, _back + y * _w + r.x1, rowBytes);
    }
    lv_area_t a;
    a.x1 = screenX + r.x1;
    a.y1 = screenY + r.y1;
    a.x2 = screenX + r.x2;
    a.y2 = screenY + r.y2;
    lv_obj_invalidate_area(canvasObj, &a);
  }
  _dirtyCount = 0;
}
