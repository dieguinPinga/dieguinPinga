// PRRR BITCOIN - Waveshare ESP32-S3-Touch-LCD-7 (800x480)
// Constantes del proyecto. Los valores marcados "(orig)" vienen sin cambios
// del sketch ST7789 172x320; los marcados "(7in)" se adaptaron a la pantalla nueva.
#pragma once
#include <Arduino.h>

// -------------------- Etapas (para probar paso a paso) --------------------
// Etapa 1: precio en vivo + grafico de precio  -> todo en 0
// Etapa 2: + SMA 25/55 y LONG/SHORT              -> PRRR_ENABLE_SMA 1
// Etapa 3: + rio de flujo                         -> PRRR_ENABLE_FLOW_BAND 1
// Etapa 4: + waterfall                            -> PRRR_ENABLE_WATERFALL 1
#define PRRR_ENABLE_SMA        1
#define PRRR_ENABLE_FLOW_BAND  1
#define PRRR_ENABLE_WATERFALL  1

// Resumen por Serial cada N ms (0 = desactivado). Nunca se imprime cada trade.
#define PRRR_SERIAL_SUMMARY_MS 10000UL

// -------------------- Colores RGB565 (orig) --------------------
#define COLOR_BLACK      0x0000
#define COLOR_WHITE      0xFFFF
#define COLOR_GREEN      0x07E0
#define COLOR_RED        0xF800
#define COLOR_GRAY       0x8410
#define COLOR_DARKGRAY   0x4208
#define COLOR_ORANGE     0xFD20
#define COLOR_CYAN       0x07FF
#define COLOR_YELLOW     0xFFE0
#define COLOR_BINANCE    0xFD20
#define COLOR_COINBASE   0x041F
#define COLOR_LIGHTGRAY  0xBDF7
#define COLOR_PRICE_UP   0x0040
#define COLOR_PRICE_UP_BRIGHT 0x07E0
#define COLOR_PRICE_DOWN 0xC000
#define COLOR_SMA_FAST   0x001F  // test azul puro; verdes SMA para probar despues: 0x03E0, 0x07C0, 0x07E0, 0x5FE0, 0x87E0, 0xBFE0
#define COLOR_SMA_FAST_DARK 0x0010
#define COLOR_SMA_SLOW   0xF900
#define COLOR_SMA_SLOW_DARK 0x8000

// -------------------- SMA (orig) --------------------
#define SMA_FAST_PERIOD 25
#define SMA_SLOW_PERIOD 55

// -------------------- "LED" (orig, ahora virtual en pantalla) --------------------
const uint8_t LED_MAX_BRIGHTNESS = 170;
const float LED_LERP_ALPHA = 0.15f;
const float LED_GAMMA = 0.55f;
const float CHART_RANGE_PAD_PCT = 0.08f;

// -------------------- Tiempos (orig salvo indicado) --------------------
const unsigned long drawIntervalMs = 280;        // textos de cabecera
const unsigned long stalePriceMs = 15000;
const unsigned long flowCalcIntervalMs = 120;
const unsigned long liveTailIntervalMs = 120;
const unsigned long flowDrawIntervalMs = 240;    // (7in) orig 120: el rio+waterfall ahora cubren ~200 KB por cuadro
const unsigned long fullRedrawMinIntervalMs = 120; // (7in) limite de redibujados completos del grafico
const unsigned long ledUpdateIntervalMs = 50;
const float FLOW_BAR_MAX_BTCS = 2.0f;  // subir si en ventanas largas el rio queda muy gordo

// -------------------- Pantalla (7in) --------------------
#define SCREEN_W 800
#define SCREEN_H 480

// Zona dibujada por pixeles (lienzo LVGL, doble buffer en PSRAM).
// Todo lo de abajo esta en coordenadas LOCALES del lienzo.
#define CANVAS_X 0
#define CANVAS_Y 124
#define CANVAS_W 800
#define CANVAS_H 304

// Grafico de precio
const int CHART_COLUMN_W = 4;   // 1, 2, 3 o 4 (orig 4)
const int CHART_INNER_W = 532;
const int CHART_COLUMNS = CHART_INNER_W / CHART_COLUMN_W;   // 133 columnas visibles
const int CHART_BOX_X = 4;
const int CHART_BOX_Y = 0;
const int CHART_BOX_W = CHART_INNER_W + 2;
const int CHART_BOX_H = 220;
const int CHART_INNER_X = CHART_BOX_X + 1;
const int CHART_INNER_Y = CHART_BOX_Y + 1;
const int CHART_INNER_H = CHART_BOX_H - 2;
const int PRICE_MARKER_W = 10;  // orig 7
const int PRICE_MARKER_X = CHART_BOX_X + CHART_BOX_W + 1;
const int PRICE_LABEL_X = PRICE_MARKER_X + PRICE_MARKER_W + 3;  // etiquetas de escala (LVGL)

// Rio de flujo (alineado columna a columna con el grafico)
const int FLOW_BOX_X = CHART_BOX_X;
const int FLOW_BOX_Y = CHART_BOX_Y + CHART_BOX_H + 6;
const int FLOW_BOX_W = CHART_BOX_W;
const int FLOW_BOX_H = CANVAS_H - FLOW_BOX_Y;
const int FLOW_INNER_X = FLOW_BOX_X + 1;
const int FLOW_INNER_Y = FLOW_BOX_Y + 1;
const int FLOW_INNER_W = FLOW_BOX_W - 2;
const int FLOW_INNER_H = FLOW_BOX_H - 2;

// Waterfall (historia arriba, frente en vivo abajo)
const int WF_BOX_X = 620;
const int WF_BOX_W = CANVAS_W - WF_BOX_X - 4;
const int WF_HIST_BOX_Y = 0;
const int WF_HIST_BOX_H = 196;
const int WF_LIVE_BOX_Y = WF_HIST_BOX_Y + WF_HIST_BOX_H + 6;
const int WF_LIVE_BOX_H = CANVAS_H - WF_LIVE_BOX_Y;

// -------------------- Ventanas temporales (tactil) --------------------
// El original usaba CHART_WINDOW_SECONDS = 14400 y CHART_WINDOW_MS = 14400 * 120,
// es decir, el PRECIO cubria 1.728.000 ms (28:48) mientras el RIO DE FLUJO cubria
// 14400 s (4 h). El preset "ORIG" reproduce exactamente eso; el resto usa la misma
// ventana para precio y flujo.
struct WindowPreset {
  const char* label;
  unsigned long priceWindowMs;
  unsigned long flowWindowSeconds;
};

static const WindowPreset WINDOW_PRESETS[] = {
  { "1m",   60000UL,          60UL    },
  { "5m",   300000UL,         300UL   },
  { "15m",  900000UL,         900UL   },
  { "1h",   3600000UL,        3600UL  },
  { "4h",   14400000UL,       14400UL },
  { "ORIG", 14400UL * 120UL,  14400UL },
};
const int WINDOW_PRESET_COUNT = sizeof(WINDOW_PRESETS) / sizeof(WINDOW_PRESETS[0]);
const int DEFAULT_WINDOW_INDEX = 5;  // arranca igual que el original

// -------------------- Buffers --------------------
const int SMA_HISTORY_PAD = SMA_SLOW_PERIOD;
const int BUCKET_STORAGE_COLUMNS = CHART_COLUMNS + SMA_HISTORY_PAD;
const int FLOW_SECONDS_SLOTS = 14400;   // maximo flowWindowSeconds de los presets
const int FLOW_LIVE_SLOTS = 24;
// Historial de precio por segundo para reconstruir el grafico al cambiar de ventana:
// 4 h visibles + 55 columnas de SMA a 108 s = ~5,6 h. En PSRAM.
const int PRICE_SECONDS_SLOTS = 21600;
