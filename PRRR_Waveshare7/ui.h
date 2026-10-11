// Interfaz LVGL: textos, indicadores y botones tactiles sobre el lienzo.
// uiInit() y uiApply() SOLO se llaman con esp_lv_adapter_lock() tomado.
// UiSnapshot se arma fuera del lock (en loop), asi el lock dura lo minimo.
#pragma once
#include <Arduino.h>
#include <lvgl.h>

#define UI_SCALE_LABELS 8

struct UiSnapshot {
  char price[28];
  uint16_t priceColor;
  uint16_t btcColor;
  uint8_t ledR, ledG, ledB;
  char pct[24];
  uint16_t pctColor;
  char highLow[64];
  char flow[28];
  int trend;               // 0 flat, 1 long, 2 short
  char binLine[64];
  uint16_t binDotColor;
  char cbLine[64];
  uint16_t cbDotColor;
  bool weightsValid;
  float binWeight;         // 0..1
  bool aggressorValid;
  float buyRatio;          // 0..1
  char aggressor[40];
  char windowInfo[64];
  char status[112];
  bool waiting;
  int activeWindow;
  int scaleCount;
  int scaleY[UI_SCALE_LABELS];          // y en pantalla del centro de la etiqueta
  char scaleText[UI_SCALE_LABELS][14];
};

void uiInit(lv_color_t* canvasBuffer, int canvasW, int canvasH);
lv_obj_t* uiCanvas();
void uiApply(const UiSnapshot& s);
// Ventana pedida por el tactil (-1 si no hay pedido). Seguro entre tareas.
int uiTakeRequestedWindow();
