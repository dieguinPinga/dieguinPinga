// =====================================================================
//  PRRR BITCOIN - Waveshare ESP32-S3-Touch-LCD-7 (800x480, ST7262 RGB, GT911)
//  Port del monitor PRRR (ESP32 + ST7789 172x320) a LVGL 8.3 + ESP32_Display_Panel.
//
//  Arquitectura:
//   - Tarea LVGL (creada por lvgl_v8_port): refresco de pantalla y tactil.
//   - Tarea loop(): WiFi + WebSockets (callbacks -> market.cpp), calculos y
//     dibujo por pixeles en un buffer privado (Canvas565, PSRAM).
//   - Unico punto de contacto: publishFrame(), que con lvgl_port_lock() copia
//     las zonas modificadas al lv_canvas y actualiza textos. Ningun callback de
//     red toca LVGL; el tactil solo deja un pedido atomico (uiTakeRequestedWindow).
// =====================================================================
#include <Arduino.h>
#include <esp_display_panel.hpp>
#include <lvgl.h>
#include <esp_heap_caps.h>

#if __has_include("lvgl_v8_port.h")
#include "lvgl_v8_port.h"
#else
#error "Falta lvgl_v8_port.h/.cpp: copialos desde tu ejemplo 09_lvgl_v8_demo (ver README.md)."
#endif

#include "prrr_config.h"
#include "market.h"
#include "canvas565.h"
#include "render.h"
#include "ui.h"
#include "feeds.h"

using namespace esp_panel::drivers;
using namespace esp_panel::board;

static Board* board = nullptr;
static Canvas565 canvas;
static bool uiReady = false;

static unsigned long lastDraw = 0;
static unsigned long lastLiveTailDraw = 0;
static unsigned long lastFullDraw = 0;
static unsigned long lastFlowDraw = 0;
static unsigned long lastLedUpdate = 0;
static unsigned long lastSummary = 0;

// -------------------- Formato (orig) --------------------
// Separador de miles "." y decimal "," (formato AR).
static void formatAR(double value, int decimals, char* outBuf, size_t bufSize) {
  char tmp[32];
  snprintf(tmp, sizeof(tmp), "%.*f", decimals, value);
  char* dot = strchr(tmp, '.');
  char intPart[20];
  char decPart[8] = {0};

  if (dot) {
    size_t n = dot - tmp;
    if (n >= sizeof(intPart)) n = sizeof(intPart) - 1;
    memcpy(intPart, tmp, n);
    intPart[n] = '\0';
    strncpy(decPart, dot + 1, sizeof(decPart) - 1);
  } else {
    strncpy(intPart, tmp, sizeof(intPart) - 1);
    intPart[sizeof(intPart) - 1] = '\0';
  }

  bool neg = (intPart[0] == '-');
  char* digits = neg ? intPart + 1 : intPart;

  char intWithDots[24];
  int len = strlen(digits);
  int outIdx = 0;
  for (int i = 0; i < len && outIdx < (int)sizeof(intWithDots) - 2; i++) {
    if (i > 0 && ((len - i) % 3 == 0)) intWithDots[outIdx++] = '.';
    intWithDots[outIdx++] = digits[i];
  }
  intWithDots[outIdx] = '\0';

  if (decimals > 0) {
    snprintf(outBuf, bufSize, "%s%s,%s", neg ? "-" : "", intWithDots, decPart);
  } else {
    snprintf(outBuf, bufSize, "%s%s", neg ? "-" : "", intWithDots);
  }
}

static void formatDuration(unsigned long totalSec, char* out, size_t n) {
  unsigned long hh = totalSec / 3600UL;
  unsigned long mm = (totalSec / 60UL) % 60UL;
  unsigned long ss = totalSec % 60UL;
  if (hh > 0) snprintf(out, n, "%lu:%02lu:%02lu", hh, mm, ss);
  else snprintf(out, n, "%lu:%02lu", mm, ss);
}

static uint16_t exchangeDotColor(const ExchangeData& ex) {
  if (!ex.wsConnected) return COLOR_RED;
  if (!isFresh(ex)) return COLOR_YELLOW;
  return COLOR_GREEN;
}

static void formatExchangeLine(const char* name, const ExchangeData& ex, float shownWeight, bool weightKnown,
                               char* out, size_t n) {
  if (ex.livePrice > 0) {
    char p[24];
    formatAR(ex.livePrice, 2, p, sizeof(p));
    if (weightKnown) snprintf(out, n, "%s  %s  %d%%", name, p, (int)(shownWeight * 100.0f + 0.5f));
    else snprintf(out, n, "%s  %s  --", name, p);
  } else {
    snprintf(out, n, "%s  %s", name, ex.wsConnected ? "esperando" : "desconectado");
  }
}

// Escala de precios con valores redondos (complementa los ticks del original).
static void buildScaleLabels(UiSnapshot& s) {
  s.scaleCount = 0;
  if (histCount < 2) return;
  double minP, maxP;
  computeChartRange(&minP, &maxP);
  double span = maxP - minP;
  static const double steps[] = { 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000 };
  double step = steps[sizeof(steps) / sizeof(steps[0]) - 1];
  for (double st : steps) {
    if (span / st <= 6.0) { step = st; break; }
  }
  for (double v = ceil(minP / step) * step; v <= maxP && s.scaleCount < UI_SCALE_LABELS; v += step) {
    int yLocal = chartYForPrice(v, minP, maxP, CHART_INNER_Y, CHART_INNER_H);
    if (yLocal < CHART_INNER_Y + 8 || yLocal > CHART_INNER_Y + CHART_INNER_H - 8) continue;
    s.scaleY[s.scaleCount] = CANVAS_Y + yLocal;
    formatAR(v, 0, s.scaleText[s.scaleCount], sizeof(s.scaleText[0]));
    s.scaleCount++;
  }
}

// Arma todos los textos de la interfaz (fuera del lock de LVGL).
static void buildSnapshot(UiSnapshot& s) {
  // Precio (orig drawHeader)
  s.priceColor = COLOR_WHITE;
  if (weightedPrice > 0 && prevWeightedPrice > 0) {
    if (weightedPrice > prevWeightedPrice) s.priceColor = COLOR_GREEN;
    else if (weightedPrice < prevWeightedPrice) s.priceColor = COLOR_RED;
  }
  if (weightedPrice > 0) {
    char priceBuf[24];
    formatAR(weightedPrice, 2, priceBuf, sizeof(priceBuf));
    snprintf(s.price, sizeof(s.price), "$%s", priceBuf);
  } else {
    strcpy(s.price, "---");
    s.priceColor = COLOR_WHITE;
  }
  s.btcColor = btcLabelColorFromRange();
  s.ledR = ledR;
  s.ledG = ledG;
  s.ledB = ledB;

  // Variacion respecto de la primera columna visible (orig)
  s.pct[0] = '\0';
  s.pctColor = COLOR_GRAY;
  int firstValidIdx = firstValidHistoryIndex();
  if (weightedPrice > 0 && firstValidIdx >= 0 && history[firstValidIdx] > 0) {
    double base = history[firstValidIdx];
    double pct = ((weightedPrice - base) / base) * 100.0;
    if (pct > 0) s.pctColor = COLOR_GREEN;
    else if (pct < 0) s.pctColor = COLOR_RED;
    char pctNum[16];
    formatAR(fabs(pct), 2, pctNum, sizeof(pctNum));
    snprintf(s.pct, sizeof(s.pct), "%s%s%%", pct >= 0 ? "+" : "-", pctNum);
  }

  s.highLow[0] = '\0';
  double lo, hi;
  if (computeVisibleHighLow(&lo, &hi)) {
    char hiBuf[16], loBuf[16];
    formatAR(hi, 0, hiBuf, sizeof(hiBuf));
    formatAR(lo, 0, loBuf, sizeof(loBuf));
    snprintf(s.highLow, sizeof(s.highLow), "#00FF00 H:%s#   #FF0000 L:%s#", hiBuf, loBuf);
  }

  char flowNum[16];
  formatAR(totalFlowBTCs, 2, flowNum, sizeof(flowNum));
  snprintf(s.flow, sizeof(s.flow), "BTC/s %s", flowNum);

#if PRRR_ENABLE_SMA
  s.trend = (currentTrendSignal == SIGNAL_LONG) ? 1 : (currentTrendSignal == SIGNAL_SHORT ? 2 : 0);
#else
  s.trend = 0;
#endif

  // Exchanges y ponderacion
  bool bFresh = isFresh(binance);
  bool cFresh = isFresh(coinbase);
  float binW = 0.5f;
  s.weightsValid = bFresh || cFresh;
  if (bFresh && cFresh) binW = binance.weight;
  else if (bFresh) binW = 1.0f;
  else if (cFresh) binW = 0.0f;
  s.binWeight = binW;
  formatExchangeLine("BINANCE", binance, binW, s.weightsValid, s.binLine, sizeof(s.binLine));
  formatExchangeLine("COINBASE", coinbase, 1.0f - binW, s.weightsValid, s.cbLine, sizeof(s.cbLine));
  s.binDotColor = exchangeDotColor(binance);
  s.cbDotColor = exchangeDotColor(coinbase);

  float aggrTotal = aggressorBuyBTCs + aggressorSellBTCs;
  s.aggressorValid = aggrTotal > 0.0001f;
  s.buyRatio = s.aggressorValid ? aggressorBuyBTCs / aggrTotal : 0.5f;
  if (s.aggressorValid) snprintf(s.aggressor, sizeof(s.aggressor), "COMPRA %d%%", (int)(s.buyRatio * 100.0f + 0.5f));
  else strcpy(s.aggressor, "AGRESOR");

  char win[16], col[16];
  formatDuration(chartWindowMs / 1000UL, win, sizeof(win));
  formatAR(bucketMs / 1000.0, 1, col, sizeof(col));
  if (flowWindowSeconds * 1000UL != chartWindowMs) {
    char fw[16];
    formatDuration(flowWindowSeconds, fw, sizeof(fw));
    snprintf(s.windowInfo, sizeof(s.windowInfo), "%s  %ss/col  flujo %s", win, col, fw);
  } else {
    snprintf(s.windowInfo, sizeof(s.windowInfo), "Ventana %s  %ss/col", win, col);
  }

  const char* ssid = feedsWifiSsid();
  snprintf(s.status, sizeof(s.status), "%s | %s%s%s", statusText.c_str(), detailText.c_str(),
           ssid[0] ? " | WiFi " : "", ssid);

  s.waiting = histCount < 2;
  s.activeWindow = currentWindowIndex;
  buildScaleLabels(s);
}

// Unico lugar donde loop() toca LVGL.
static bool publishFrame(bool withHeader, int lockTimeoutMs) {
  static UiSnapshot snap;
  if (withHeader) buildSnapshot(snap);
  if (!lvgl_port_lock(lockTimeoutMs)) return false;
  canvas.publish(uiCanvas(), CANVAS_X, CANVAS_Y);
  if (withHeader) uiApply(snap);
  lvgl_port_unlock();
  return true;
}

// Llamada por feeds.cpp mientras connectWiFi() bloquea.
void appPublishStatus() {
  if (!uiReady) return;
  publishFrame(true, 200);
}

static void printSummary(unsigned long nowMs) {
#if PRRR_SERIAL_SUMMARY_MS > 0
  if (nowMs - lastSummary < PRRR_SERIAL_SUMMARY_MS) return;
  float dt = (lastSummary == 0) ? 1.0f : (nowMs - lastSummary) / 1000.0f;
  lastSummary = nowMs;
  static uint32_t prevBin = 0, prevCb = 0;
  uint32_t dBin = tradeCountBin - prevBin;
  uint32_t dCb = tradeCountCb - prevCb;
  prevBin = tradeCountBin;
  prevCb = tradeCountCb;
  Serial.printf("[PRRR] precio=%.2f BIN=%s %.2f (%.1f msg/s) CB=%s %.2f (%.1f msg/s) flujo=%.3f BTC/s cols=%d senal=%d "
                "heap=%u psram=%u\n",
                weightedPrice,
                binance.wsConnected ? "on" : "off", binance.livePrice, dBin / dt,
                coinbase.wsConnected ? "on" : "off", coinbase.livePrice, dCb / dt,
                totalFlowBTCs, histCount, (int)currentTrendSignal,
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
#endif
}

static void haltWithError(const char* msg) {
  Serial.println(msg);
  while (true) delay(1000);
}

// -------------------- Setup / Loop --------------------
void setup() {
  // (7in) Sin setCpuFrequencyMhz(80): el bus RGB necesita la CPU a 240 MHz.
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[PRRR] Waveshare ESP32-S3-Touch-LCD-7");

  if (!psramFound()) haltWithError("[PRRR] ERROR: PSRAM no detectada. Tools > PSRAM: OPI PSRAM / Enabled.");

  // Inicializacion de pantalla tomada del ejemplo lvgl_v8_port de ESP32_Display_Panel
  Serial.println("[PRRR] Inicializando placa");
  board = new Board();
  board->init();
#if LVGL_PORT_AVOID_TEARING_MODE
  auto lcd = board->getLCD();
  lcd->configFrameBufferNumber(LVGL_PORT_DISP_BUFFER_NUM);
#if ESP_PANEL_DRIVERS_BUS_ENABLE_RGB && CONFIG_IDF_TARGET_ESP32S3
  auto lcd_bus = lcd->getBus();
  if (lcd_bus->getBasicAttributes().type == ESP_PANEL_BUS_TYPE_RGB) {
    static_cast<BusRGB*>(lcd_bus)->configRGB_BounceBufferSize(lcd->getFrameWidth() * 10);
  }
#endif
#endif
  if (!board->begin()) haltWithError("[PRRR] ERROR: board->begin() fallo. Revisar esp_panel_board_supported_conf.h");

  Serial.println("[PRRR] Inicializando LVGL");
  lvgl_port_init(board->getLCD(), board->getTouch());

  if (!marketInit()) haltWithError("[PRRR] ERROR: sin PSRAM para el historial");
  if (!canvas.begin(CANVAS_W, CANVAS_H)) haltWithError("[PRRR] ERROR: sin PSRAM para el lienzo");
  renderBegin(&canvas);

  lvgl_port_lock(-1);
  uiInit(canvas.frontBuffer(), CANVAS_W, CANVAS_H);
  lvgl_port_unlock();
  uiReady = true;

  drawStaticFrames();
  drawChartArea();
  publishFrame(true, -1);

  feedsBegin();
  lastDraw = millis();
}

void loop() {
  // 1) Red: los callbacks solo actualizan el modelo
  feedsLoop();
  updateFlows();

  // 2) Pedido del tactil (llega desde la tarea LVGL de forma atomica)
  int req = uiTakeRequestedWindow();
  if (req >= 0 && req != currentWindowIndex) {
    marketApplyWindow(req);
    lastDraw = 0;  // refrescar textos ya
    Serial.printf("[PRRR] Ventana -> %s\n", WINDOW_PRESETS[req].label);
  }

  unsigned long nowMs = millis();
  if (nowMs - lastLedUpdate >= ledUpdateIntervalMs) {
    lastLedUpdate = nowMs;
    updateVirtualLed();
  }

  // 3) Dibujo en el buffer privado (sin LVGL)
  bool liveDue = (nowMs - lastLiveTailDraw >= liveTailIntervalMs);
  if (chartDirty) {
    if (nowMs - lastFullDraw >= fullRedrawMinIntervalMs) {
      drawChartArea();
      lastFullDraw = nowMs;
      liveDue = true;
    }
  } else if (liveDue) {
    drawTailRegionDefault();  // ultima columna (chartTailDirty) al ritmo del marcador en vivo
  }
  if (liveDue && !chartDirty) {
    lastLiveTailDraw = nowMs;
    drawLivePriceTailDefault();
  }

  if (flowDirty && (nowMs - lastFlowDraw >= flowDrawIntervalMs)) {
    lastFlowDraw = nowMs;
    drawFlowBandArea();
    drawWaterfallArea();
    flowDirty = false;
  }

  // 4) Publicar en LVGL (lock corto; si esta ocupado se reintenta en la proxima vuelta)
  bool headerDue = (nowMs - lastDraw >= drawIntervalMs);
  if (canvas.hasDirty() || headerDue) {
    if (publishFrame(headerDue, 20) && headerDue) lastDraw = nowMs;
  }

  printSummary(nowMs);
  delay(2);
}
