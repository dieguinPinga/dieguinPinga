// Lienzo RGB565 con doble buffer en PSRAM.
// Reemplaza a Arduino_GFX: ofrece las mismas primitivas que usaba el original
// (drawPixel, drawFastHLine, drawLine, fillRect...) pero dibuja en un buffer
// "trasero" privado. publish() copia solo los rectangulos modificados al buffer
// del lv_canvas e invalida esas zonas; debe llamarse con el lock de LVGL tomado.
// Asi el dibujo (lento) no bloquea a LVGL y LVGL nunca ve un cuadro a medio dibujar.
#pragma once
#include <Arduino.h>
#include <lvgl.h>

#if LV_COLOR_DEPTH != 16
#error "PRRR requiere LV_COLOR_DEPTH 16 en lv_conf.h"
#endif

class Canvas565 {
public:
  bool begin(int w, int h);
  int width() const { return _w; }
  int height() const { return _h; }
  lv_color_t* frontBuffer() const { return _front; }

  inline void drawPixel(int x, int y, uint16_t c) {
    if ((unsigned)x >= (unsigned)_w || (unsigned)y >= (unsigned)_h) return;
    _back[y * _w + x] = toLv(c);
  }
  void drawFastHLine(int x, int y, int w, uint16_t c);
  void drawFastVLine(int x, int y, int h, uint16_t c);
  void fillRect(int x, int y, int w, int h, uint16_t c);
  void drawRect(int x, int y, int w, int h, uint16_t c);
  void drawLine(int x0, int y0, int x1, int y1, uint16_t c);

  // Marca una zona para copiar en el proximo publish().
  void markDirty(int x, int y, int w, int h);
  bool hasDirty() const { return _dirtyCount > 0; }
  // Requiere esp_lv_adapter_lock() tomado por el llamador.
  // Devuelve los bytes copiados (diagnostico).
  size_t publish(lv_obj_t* canvasObj, int screenX, int screenY);

  static inline lv_color_t toLv(uint16_t c) {
    lv_color_t o;
#if LV_COLOR_16_SWAP
    o.full = (uint16_t)((c >> 8) | (c << 8));
#else
    o.full = c;
#endif
    return o;
  }

private:
  struct Rect { int x1, y1, x2, y2; };
  static const int MAX_DIRTY = 8;
  lv_color_t* _back = nullptr;
  lv_color_t* _front = nullptr;
  int _w = 0;
  int _h = 0;
  Rect _dirty[MAX_DIRTY];
  int _dirtyCount = 0;
};
