#include <Arduino_GFX_Library.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <Adafruit_NeoPixel.h>
#include "esp32-hal-cpu.h"

#define BL_PIN 22

// -------------------- LED --------------------
#define LED_PIN    8
#define LED_COUNT  1
Adafruit_NeoPixel led(LED_COUNT, LED_PIN, NEO_RGB + NEO_KHZ800);

const uint8_t LED_MAX_BRIGHTNESS = 170;
const float LED_LERP_ALPHA = 0.15f;
const float LED_GAMMA = 0.55f;
float ledSmoothedPos = 0.5f;
const float CHART_RANGE_PAD_PCT = 0.08f;

// -------------------- Layout / Texto --------------------
const int TEXT_MARGIN_X = 10;

// -------------------- Colores --------------------
#define COLOR_BLACK      0x0000
#define COLOR_WHITE      0xFFFF
#define COLOR_GREEN      0x07E0
#define COLOR_RED        0xF800
#define COLOR_GRAY       0x8410
#define COLOR_DARKGRAY   0x4208
#define COLOR_ORANGE     0xFD20
#define COLOR_CYAN       0x07FF
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

// -------------------- SMA --------------------0x03E0 0x07C0
#define SMA_FAST_PERIOD 25
#define SMA_SLOW_PERIOD 55

struct SmaPoint {
  bool valid;
  float value;
};

enum CrossSignal {
  SIGNAL_FLAT = 0,
  SIGNAL_LONG,
  SIGNAL_SHORT
};

CrossSignal currentTrendSignal = SIGNAL_FLAT;

// -------------------- WiFi --------------------
struct WifiNet {
  const char* ssid;
  const char* pass;
};

WifiNet wifiList[] = {
  { "Ecotecnica",    "0000011111" },
  { "CastorBionico", "0000011111" },
  { "Raul_Normal",   "Ab1234567"  },
  { "RaspiEco",      "eco12345"   }
};
const int wifiCount = sizeof(wifiList) / sizeof(wifiList[0]);
int currentWifiIdx = -1;

// -------------------- Pantalla --------------------
Arduino_DataBus *bus = new Arduino_ESP32SPI(15, 14, 7, 6, -1);
Arduino_GFX *gfx = new Arduino_ST7789(bus, 21, 1, true, 172, 320, 34, 0, 34, 0);

// -------------------- WebSockets --------------------
WebSocketsClient wsBinance;
WebSocketsClient wsCoinbase;

struct ExchangeData {
  const char* label;
  uint16_t color;
  float livePrice;
  float weight;
  bool wsConnected;
  bool priceOk;
  unsigned long lastPriceMs;
  float btcAccum;
  float flowBTCs;
  float lastTradeBTC;
};

ExchangeData binance  = { "BIN", COLOR_BINANCE,  -1, 0.5f, false, false, 0, 0, 0, 0 };
ExchangeData coinbase = { "CB",  COLOR_COINBASE, -1, 0.5f, false, false, 0, 0, 0, 0 };

// -------------------- Globales --------------------
float weightedPrice = -1.0f;
float prevWeightedPrice = -1.0f;
float firstWeightedPrice = 0.0f;
bool hasFirstWeighted = false;

String statusText = "Boot...";
String detailText = "";

unsigned long lastDraw = 0;
const unsigned long drawIntervalMs = 280;
const unsigned long stalePriceMs = 15000;

// Historial / Config rapida
// Ventana:
// 10 segundos -> 10UL
// 30 segundos -> 30UL
// 1 minuto    -> 60UL
// 5 minutos   -> 300UL
// 1 hora 3600UL
// 4 horas 14400UL
//  media hoar 1800UL


// Columnas visibles de ejemplo sobre 304 px utiles:
// ancho 1 -> 304 aprox
// ancho 2 -> 152 aprox
// ancho 3 -> 101 aprox
// ancho 4 -> 77 aprox
const unsigned long CHART_WINDOW_SECONDS = 60UL;
const unsigned long CHART_WINDOW_MS = CHART_WINDOW_SECONDS * 1000UL;
const int CHART_COLUMN_W = 2;   // 1, 2, 3 o 4
const int CHART_COLUMNS = 154;   // visible
const int SMA_HISTORY_PAD = SMA_SLOW_PERIOD;
const int BUCKET_STORAGE_COLUMNS = CHART_COLUMNS + SMA_HISTORY_PAD;
const unsigned long BUCKET_MS = CHART_WINDOW_MS / CHART_COLUMNS;
const int FLOW_SECONDS_SLOTS = (int)CHART_WINDOW_SECONDS;
const int FLOW_LIVE_SLOTS = 24;
const int PRICE_MARKER_W = 7;
float history[CHART_COLUMNS];
float flowHistory[CHART_COLUMNS];
bool historyValid[CHART_COLUMNS];
unsigned long historyBucketIds[CHART_COLUMNS];
float bucketPrice[BUCKET_STORAGE_COLUMNS];
float bucketFlow[BUCKET_STORAGE_COLUMNS];
bool bucketValid[BUCKET_STORAGE_COLUMNS];
unsigned long bucketIds[BUCKET_STORAGE_COLUMNS];
float flowSecondBuckets[FLOW_SECONDS_SLOTS];
bool flowSecondValid[FLOW_SECONDS_SLOTS];
unsigned long flowSecondIds[FLOW_SECONDS_SLOTS];
float flowLiveSamples[FLOW_LIVE_SLOTS];
bool flowLiveValid[FLOW_LIVE_SLOTS];
unsigned long flowLiveIds[FLOW_LIVE_SLOTS];
int histCount = 0;
int newestValidIndex = -1;

// Flow
unsigned long lastFlowCalc = 0;
const unsigned long flowCalcIntervalMs = 120;  // mas fluido; seguimos normalizando a BTC/s
const unsigned long flowDrawIntervalMs = 120;
float totalFlowBTCs = 0;
const float FLOW_BAR_MAX_BTCS = 2.0f;  // subir si en ventanas largas el rio queda muy gordo

// Optimización
bool chartDirty = true;
bool chartTailDirty = false;
unsigned long lastLiveTailDraw = 0;
const unsigned long liveTailIntervalMs = 120;
unsigned long lastLiveFlowDraw = 0;
unsigned long lastVisibleNewestBucketId = 0;
unsigned long chartLogicalBucketId = 0;
unsigned long lastBucketAdvanceMs = 0;

// Geometria chart
const int CHART_BOX_X = 5;
const int CHART_BOX_Y = 32;
const int CHART_BOX_W = 306;
const int CHART_BOX_H = 139;
const int CHART_INNER_X = 6;
const int CHART_INNER_Y = 33;
const int CHART_INNER_W = 304;
const int CHART_INNER_H = 137;
const int PRICE_MARKER_X = CHART_BOX_X + CHART_BOX_W + 1;

// -------------------- Helpers --------------------
bool isFresh(const ExchangeData& ex) {
  return ex.priceOk && (millis() - ex.lastPriceMs < stalePriceMs);
}

bool hasFreshWeightedPrice() {
  return weightedPrice > 0.0f && (isFresh(binance) || isFresh(coinbase));
}

unsigned long currentChartBucketId() {
  return chartLogicalBucketId;
}

int firstValidHistoryIndex() {
  for (int i = 0; i < CHART_COLUMNS; i++) {
    if (historyValid[i]) return i;
  }
  return -1;
}

void formatAR(float value, int decimals, char* outBuf, size_t bufSize) {
  char tmp[24];
  snprintf(tmp, sizeof(tmp), "%.*f", decimals, value);
  char* dot = strchr(tmp, '.');
  char intPart[16];
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

  char intWithDots[20];
  int len = strlen(digits);
  int outIdx = 0;
  for (int i = 0; i < len; i++) {
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

int textWidthPx(const char* s, int textSize) {
  return strlen(s) * 6 * textSize;
}

int plotWidthPx(int w) {
  return w;
}

uint16_t lerp565(uint16_t c1, uint16_t c2, float t) {
  if (t < 0) t = 0;
  if (t > 1) t = 1;
  uint8_t r1 = (c1 >> 11) & 0x1F;
  uint8_t g1 = (c1 >> 5) & 0x3F;
  uint8_t b1 = c1 & 0x1F;
  uint8_t r2 = (c2 >> 11) & 0x1F;
  uint8_t g2 = (c2 >> 5) & 0x3F;
  uint8_t b2 = c2 & 0x1F;
  uint8_t r = r1 + (int)((r2 - r1) * t);
  uint8_t g = g1 + (int)((g2 - g1) * t);
  uint8_t b = b1 + (int)((b2 - b1) * t);
  return (r << 11) | (g << 5) | b;
}

uint16_t scale565(uint16_t c, float factor) {
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

void computeChartRange(float* outMin, float* outMax) {
  if (histCount <= 0) {
    *outMin = 0.0f;
    *outMax = 1.0f;
    return;
  }

  bool found = false;
  float minP = 0.0f;
  float maxP = 0.0f;
  for (int i = 0; i < CHART_COLUMNS; i++) {
    if (!historyValid[i]) continue;
    if (!found) {
      minP = history[i];
      maxP = history[i];
      found = true;
      continue;
    }
    if (history[i] < minP) minP = history[i];
    if (history[i] > maxP) maxP = history[i];
  }
  if (!found) {
    *outMin = 0.0f;
    *outMax = 1.0f;
    return;
  }
  if (weightedPrice > 0) {
    if (weightedPrice < minP) minP = weightedPrice;
    if (weightedPrice > maxP) maxP = weightedPrice;
  }
  if ((maxP - minP) < 0.0001f) maxP = minP + 1.0f;
  float pad = (maxP - minP) * CHART_RANGE_PAD_PCT;
  if (pad < 1.0f) pad = 1.0f;
  minP -= pad;
  maxP += pad;
  *outMin = minP;
  *outMax = maxP;
}

bool chartRangeChanged(float oldMin, float oldMax, float newMin, float newMax) {
  float oldSpan = oldMax - oldMin;
  float newSpan = newMax - newMin;
  if (oldSpan < 0.0001f) oldSpan = 1.0f;
  if (newSpan < 0.0001f) newSpan = 1.0f;

  float minShift = fabs(newMin - oldMin);
  float maxShift = fabs(newMax - oldMax);

  return (minShift > (oldSpan * 0.02f)) || (maxShift > (oldSpan * 0.02f)) || (fabs(newSpan - oldSpan) > (oldSpan * 0.03f));
}

int chartXForIndex(int idx, int x, int w) {
  int plotW = plotWidthPx(w);
  int px = x + (idx * CHART_COLUMN_W);
  int maxX = x + plotW - CHART_COLUMN_W;
  if (px > maxX) px = maxX;
  return px;
}

int chartYForPrice(float price, float minP, float maxP, int y, int h) {
  float range = maxP - minP;
  if (range < 0.0001f) range = 1.0f;
  return y + h - 2 - (int)((price - minP) / range * (h - 4));
}

int clampChartInnerY(int py, int y, int h) {
  int minY = y + 1;
  int maxY = y + h - 3;
  if (py < minY) py = minY;
  if (py > maxY) py = maxY;
  return py;
}

bool calcSMAAt(int idx, int period, float* outAvg) {
  if (idx < 0 || idx >= CHART_COLUMNS || !historyValid[idx]) return false;
  float sum = 0.0f;
  int samples = 0;
  unsigned long bucketId = historyBucketIds[idx];

  for (int offset = 0; offset < BUCKET_STORAGE_COLUMNS; offset++) {
    unsigned long sourceBucket = bucketId - offset;
    int slot = (int)(sourceBucket % BUCKET_STORAGE_COLUMNS);
    if (!bucketValid[slot] || bucketIds[slot] != sourceBucket) continue;
    sum += bucketPrice[slot];
    samples++;
    if (samples >= period) break;
  }
  if (samples < period) return false;
  *outAvg = sum / period;
  return true;
}

void drawLine2px(int x1, int y1, int x2, int y2, uint16_t color) {
  gfx->drawLine(x1, y1, x2, y2, color);
  gfx->drawLine(x1, y1 + 1, x2, y2 + 1, color);
}

void drawLine3px(int x1, int y1, int x2, int y2, uint16_t color) {
  gfx->drawLine(x1, y1 - 1, x2, y2 - 1, color);
  gfx->drawLine(x1, y1, x2, y2, color);
  gfx->drawLine(x1, y1 + 1, x2, y2 + 1, color);
}

void drawDualStrokeLine(int x1, int y1, int x2, int y2, uint16_t mainColor, uint16_t darkColor) {
  gfx->drawLine(x1, y1, x2, y2, mainColor);
  gfx->drawLine(x1 + 1, y1, x2 + 1, y2, darkColor);
}

void drawDualStrokePoint(int x, int y, int w, uint16_t mainColor, uint16_t darkColor) {
  int mainW = w / 2;
  if (mainW < 1) mainW = 1;
  int darkW = w - mainW;
  if (darkW < 1) darkW = 1;
  gfx->drawFastHLine(x, y, mainW, mainColor);
  gfx->drawFastHLine(x + mainW, y, darkW, darkColor);
}

void drawPriceStepLink(int prevX, int prevY, int currX, int currY, uint16_t color) {
  int leftX = (currX < prevX) ? currX : prevX;
  int rightX = (currX > prevX) ? currX : prevX;
  uint16_t accentColor = (color == COLOR_PRICE_UP) ? COLOR_PRICE_UP_BRIGHT : color;
  gfx->drawFastHLine(leftX, prevY, (rightX - leftX) + CHART_COLUMN_W, color);
  gfx->drawFastHLine(leftX, prevY + 1, (rightX - leftX) + CHART_COLUMN_W, accentColor);

  int topY = (currY < prevY) ? currY : prevY;
  int height = abs(currY - prevY) + 2;
  int splitW = CHART_COLUMN_W / 2;
  if (splitW < 1) splitW = 1;
  for (int yy = topY; yy < topY + height; yy++) {
    if (color == COLOR_PRICE_UP && CHART_COLUMN_W > 1) {
      gfx->drawFastHLine(currX, yy, splitW, color);
      gfx->drawFastHLine(currX + splitW, yy, CHART_COLUMN_W - splitW, accentColor);
    } else {
      uint16_t lineColor = ((yy - topY) & 0x01) ? accentColor : color;
      gfx->drawFastHLine(currX, yy, CHART_COLUMN_W, lineColor);
    }
  }
}

void drawPricePoint2px(int x, int y, uint16_t color) {
  uint16_t accentColor = (color == COLOR_PRICE_UP) ? COLOR_PRICE_UP_BRIGHT : color;
  int splitW = CHART_COLUMN_W / 2;
  if (splitW < 1) splitW = 1;
  if (color == COLOR_PRICE_UP && CHART_COLUMN_W > 1) {
    gfx->drawFastHLine(x, y, splitW, color);
    gfx->drawFastHLine(x + splitW, y, CHART_COLUMN_W - splitW, accentColor);
    gfx->drawFastHLine(x, y + 1, splitW, color);
    gfx->drawFastHLine(x + splitW, y + 1, CHART_COLUMN_W - splitW, accentColor);
  } else {
    gfx->drawFastHLine(x, y, CHART_COLUMN_W, color);
    gfx->drawFastHLine(x, y + 1, CHART_COLUMN_W, accentColor);
  }
}

void updateCrossSignal() {
  currentTrendSignal = SIGNAL_FLAT;

  float latestFast = 0.0f;
  float latestSlow = 0.0f;
  if (calcSMAAt(newestValidIndex, SMA_FAST_PERIOD, &latestFast) && calcSMAAt(newestValidIndex, SMA_SLOW_PERIOD, &latestSlow)) {
    if (latestFast > latestSlow) currentTrendSignal = SIGNAL_LONG;
    else if (latestFast < latestSlow) currentTrendSignal = SIGNAL_SHORT;
  }
}

void rebuildFlowHistoryFromSeconds() {
  unsigned long newestSecond = millis() / 1000UL;
  long long oldestSecond = (long long)newestSecond - (long long)CHART_WINDOW_SECONDS + 1LL;
  if (oldestSecond < 0) oldestSecond = 0;

  for (int i = 0; i < CHART_COLUMNS; i++) {
    unsigned long segStart = (unsigned long)(oldestSecond + ((long long)i * (long long)CHART_WINDOW_SECONDS) / CHART_COLUMNS);
    unsigned long segEnd = (unsigned long)(oldestSecond + (((long long)(i + 1) * (long long)CHART_WINDOW_SECONDS) / CHART_COLUMNS));
    if (segEnd <= segStart) segEnd = segStart + 1;

    float sum = 0.0f;
    float peak = 0.0f;
    float lastVal = 0.0f;
    bool hasLastVal = false;
    int count = 0;
    for (unsigned long sec = segStart; sec < segEnd; sec++) {
      int slot = (int)(sec % FLOW_SECONDS_SLOTS);
      if (!flowSecondValid[slot] || flowSecondIds[slot] != sec) continue;
      float v = flowSecondBuckets[slot];
      sum += v;
      if (v > peak) peak = v;
      lastVal = v;
      hasLastVal = true;
      count++;
    }

    if (count > 0) {
      float avg = sum / count;
      float lively = (avg * 0.35f) + (peak * 0.65f);
      if (hasLastVal && i == (CHART_COLUMNS - 1)) {
        lively = (lively * 0.25f) + (lastVal * 0.75f);
      }
      flowHistory[i] = lively;
    } else {
      flowHistory[i] = 0.0f;
    }
  }
}

void rebuildVisibleHistory() {
  unsigned long newestBucket = currentChartBucketId();
  unsigned long oldestBucket = newestBucket - (CHART_COLUMNS - 1);
  lastVisibleNewestBucketId = newestBucket;

  histCount = 0;
  newestValidIndex = -1;

  for (int i = 0; i < CHART_COLUMNS; i++) {
    unsigned long sourceBucket = oldestBucket + i;
    int slot = (int)(sourceBucket % BUCKET_STORAGE_COLUMNS);

    if (bucketValid[slot] && bucketIds[slot] == sourceBucket) {
      history[i] = bucketPrice[slot];
        historyValid[i] = true;
        historyBucketIds[i] = sourceBucket;
        histCount++;
        newestValidIndex = i;
      } else {
        history[i] = 0.0f;
        historyValid[i] = false;
        historyBucketIds[i] = 0;
      }
    }

  rebuildFlowHistoryFromSeconds();
  updateCrossSignal();
}

void clearChartBuckets() {
  for (int i = 0; i < CHART_COLUMNS; i++) {
    history[i] = 0.0f;
    flowHistory[i] = 0.0f;
    historyValid[i] = false;
    historyBucketIds[i] = 0;
  }
  for (int i = 0; i < BUCKET_STORAGE_COLUMNS; i++) {
    bucketPrice[i] = 0.0f;
    bucketFlow[i] = 0.0f;
    bucketValid[i] = false;
    bucketIds[i] = 0;
  }
  for (int i = 0; i < FLOW_SECONDS_SLOTS; i++) {
    flowSecondBuckets[i] = 0.0f;
    flowSecondValid[i] = false;
    flowSecondIds[i] = 0;
  }
  for (int i = 0; i < FLOW_LIVE_SLOTS; i++) {
    flowLiveSamples[i] = 0.0f;
    flowLiveValid[i] = false;
    flowLiveIds[i] = 0;
  }
  histCount = 0;
  newestValidIndex = -1;
  chartLogicalBucketId = 0;
  lastVisibleNewestBucketId = 0;
  lastBucketAdvanceMs = 0;
}

void writeBucketSample(unsigned long bucketId, float price, float flowDelta, bool accumulateFlow) {
  if (price <= 0.0f) return;

  int slot = (int)(bucketId % BUCKET_STORAGE_COLUMNS);
  bool newBucket = (!bucketValid[slot] || bucketIds[slot] != bucketId);

  if (newBucket) {
    bucketPrice[slot] = price;
    bucketFlow[slot] = flowDelta;
    bucketValid[slot] = true;
    bucketIds[slot] = bucketId;
  } else {
    bucketPrice[slot] = price;
    if (accumulateFlow && flowDelta > 0.0f) {
      // El rio representa intensidad reciente (BTC/s suavizado), no volumen acumulado del bucket.
      bucketFlow[slot] = (bucketFlow[slot] * 0.70f) + (flowDelta * 0.30f);
    }
  }
}

void ingestChartSample(float price, float flowDelta) {
  if (price <= 0.0f) return;
  unsigned long nowMs = millis();
  if (lastBucketAdvanceMs == 0) {
    lastBucketAdvanceMs = nowMs;
  } else if ((nowMs - lastBucketAdvanceMs) >= BUCKET_MS) {
    chartLogicalBucketId++;
    lastBucketAdvanceMs = nowMs;
  }

  unsigned long targetBucketId = currentChartBucketId();
  bool bucketAdvanced = (targetBucketId > lastVisibleNewestBucketId);

  float oldMin = 0.0f;
  float oldMax = 1.0f;
  computeChartRange(&oldMin, &oldMax);

  writeBucketSample(targetBucketId, price, flowDelta, true);

  if (!hasFirstWeighted) {
    firstWeightedPrice = price;
    hasFirstWeighted = true;
  }

  rebuildVisibleHistory();
  float newMin = 0.0f;
  float newMax = 1.0f;
  computeChartRange(&newMin, &newMax);

  bool needFullRedraw = bucketAdvanced || chartRangeChanged(oldMin, oldMax, newMin, newMax);
  chartDirty = needFullRedraw;
  chartTailDirty = !needFullRedraw;
}

void advanceChartTimeIfNeeded() {
  return;
}

// -------------------- LED --------------------
void updateLed() {
  if (weightedPrice <= 0 || histCount < 2) {
    led.setPixelColor(0, led.Color(0, 0, 0));
    led.show();
    return;
  }

  float minP, maxP;
  computeChartRange(&minP, &maxP);

  float range = maxP - minP;
  float pos = 0.5f;
  if (range > 0.0001f) {
    pos = (weightedPrice - minP) / range;
    if (pos < 0.0f) pos = 0.0f;
    if (pos > 1.0f) pos = 1.0f;
  }

  ledSmoothedPos += (pos - ledSmoothedPos) * LED_LERP_ALPHA;

  float p = ledSmoothedPos;
  float distFromCenter = fabs(p - 0.5f) * 2.0f;
  if (distFromCenter > 1.0f) distFromCenter = 1.0f;

  float t = powf(distFromCenter, LED_GAMMA);
  if (t < 0.0f) t = 0.0f;
  if (t > 1.0f) t = 1.0f;

  uint8_t whitePart = (uint8_t)((1.0f - t) * LED_MAX_BRIGHTNESS);

  uint8_t r, g, b;
  if (p >= 0.5f) {
    r = whitePart;
    g = LED_MAX_BRIGHTNESS;
    b = whitePart;
  } else {
    r = LED_MAX_BRIGHTNESS;
    g = whitePart;
    b = whitePart;
  }

  led.setPixelColor(0, led.Color(r, g, b));
  led.show();
}

uint16_t btcLabelColorFromRange() {
  if (weightedPrice <= 0 || histCount < 2) return COLOR_WHITE;

  float minP, maxP;
  computeChartRange(&minP, &maxP);

  float range = maxP - minP;
  float pos = 0.5f;
  if (range > 0.0001f) {
    pos = (weightedPrice - minP) / range;
    if (pos < 0.0f) pos = 0.0f;
    if (pos > 1.0f) pos = 1.0f;
  }

  float p = ledSmoothedPos + (pos - ledSmoothedPos) * 0.35f;
  if (p < 0.0f) p = 0.0f;
  if (p > 1.0f) p = 1.0f;

  float distFromCenter = fabs(p - 0.5f) * 2.0f;
  if (distFromCenter > 1.0f) distFromCenter = 1.0f;

  if (distFromCenter < 0.18f) return COLOR_WHITE;

  float t = powf(distFromCenter, LED_GAMMA);
  if (p >= 0.5f) return lerp565(COLOR_WHITE, COLOR_GREEN, t);
  return lerp565(COLOR_WHITE, COLOR_RED, t);
}

void recomputeWeightedPrice() {
  bool bFresh = isFresh(binance);
  bool cFresh = isFresh(coinbase);
  prevWeightedPrice = weightedPrice;

  if (!bFresh && !cFresh) {
    weightedPrice = -1;
    statusText = "Sin precios";
    detailText = "ambos stale";
    return;
  }
  if (bFresh && !cFresh) {
    weightedPrice = binance.livePrice;
    statusText = "Solo Binance";
    detailText = "Coinbase stale";
    return;
  }
  if (!bFresh && cFresh) {
    weightedPrice = coinbase.livePrice;
    statusText = "Solo Coinbase";
    detailText = "Binance stale";
    return;
  }

  float totalFlow = binance.flowBTCs + coinbase.flowBTCs;
  if (totalFlow > 0.0000001f) {
    binance.weight = binance.flowBTCs / totalFlow;
    coinbase.weight = coinbase.flowBTCs / totalFlow;
  } else {
    binance.weight = 0.5f;
    coinbase.weight = 0.5f;
  }

  weightedPrice = (binance.livePrice * binance.weight) + (coinbase.livePrice * coinbase.weight);
  statusText = "Ponderado x flujo";
  detailText = "BIN " + String((int)(binance.weight * 100)) + "%  CB " + String((int)(coinbase.weight * 100)) + "%";
}

void updateFlows() {
  unsigned long nowMs = millis();
  unsigned long elapsedMs = nowMs - lastFlowCalc;
  if (elapsedMs < flowCalcIntervalMs) return;
  lastFlowCalc = nowMs;

  float elapsedSec = (float)elapsedMs / 1000.0f;
  if (elapsedSec < 0.001f) elapsedSec = 0.001f;
  float toPerSecond = 1.0f / elapsedSec;

  const float alpha = 0.35f;
  float newBin = binance.btcAccum * toPerSecond;
  float newCb  = coinbase.btcAccum * toPerSecond;

  binance.flowBTCs  = (binance.flowBTCs * (1.0f - alpha)) + (newBin * alpha);
  coinbase.flowBTCs = (coinbase.flowBTCs * (1.0f - alpha)) + (newCb * alpha);

  totalFlowBTCs = binance.flowBTCs + coinbase.flowBTCs;
  binance.btcAccum = 0;
  coinbase.btcAccum = 0;
  int secondSlot = (int)((lastFlowCalc / 1000UL) % FLOW_SECONDS_SLOTS);
  unsigned long secondId = lastFlowCalc / 1000UL;
  flowSecondBuckets[secondSlot] = totalFlowBTCs;
  flowSecondValid[secondSlot] = true;
  flowSecondIds[secondSlot] = secondId;

  unsigned long liveId = lastFlowCalc / flowCalcIntervalMs;
  int liveSlot = (int)(liveId % FLOW_LIVE_SLOTS);
  flowLiveSamples[liveSlot] = totalFlowBTCs;
  flowLiveValid[liveSlot] = true;
  flowLiveIds[liveSlot] = liveId;

  rebuildFlowHistoryFromSeconds();
  chartDirty = false;
  chartTailDirty = true;
  recomputeWeightedPrice();
}

// -------------------- Dibujo --------------------
#define FLOW_GREEN_OUTER  0x00C0
#define FLOW_GREEN_MID    0x0180
#define FLOW_GREEN_CORE   0x0320
#define FLOW_RED_OUTER    0x1000
#define FLOW_RED_MID      0x2020
#define FLOW_RED_CORE     0x4020
#define FLOW_GRAY_OUTER   0x1082
#define FLOW_GRAY_MID     0x2104
#define FLOW_GRAY_CORE    0x3186

uint16_t flowGreenColor(uint8_t tone) {
  return (tone == 0) ? FLOW_GREEN_OUTER : (tone == 1 ? FLOW_GREEN_MID : FLOW_GREEN_CORE);
}

uint16_t flowRedColor(uint8_t tone) {
  return (tone == 0) ? FLOW_RED_OUTER : (tone == 1 ? FLOW_RED_MID : FLOW_RED_CORE);
}

float trendBullRatioAt(int idx) {
  if (idx <= 0 || histCount < 2 || !historyValid[idx]) return 0.5f;

  float fast = 0.0f;
  float slow = 0.0f;
  if (calcSMAAt(idx, SMA_FAST_PERIOD, &fast) && calcSMAAt(idx, SMA_SLOW_PERIOD, &slow) && slow > 0.0f) {
    float diffPct = ((fast - slow) / slow) * 100.0f;
    float ratio = 0.5f + (diffPct / 0.35f);
    if (ratio < 0.0f) ratio = 0.0f;
    if (ratio > 1.0f) ratio = 1.0f;
    return ratio;
  }

  int lookback = (idx >= 4) ? 4 : idx;
  if (lookback <= 0) return 0.5f;

  int refIdx = idx - lookback;
  while (refIdx < idx && !historyValid[refIdx]) refIdx++;
  if (refIdx >= idx) return 0.5f;

  float ref = history[refIdx];
  if (ref <= 0.0f) return 0.5f;

  float pct = ((history[idx] - ref) / ref) * 100.0f;
  float ratio = 0.5f + (pct / 0.50f);
  if (ratio < 0.0f) ratio = 0.0f;
  if (ratio > 1.0f) ratio = 1.0f;
  return ratio;
}

bool flowDitherPickGreen(int px, int py, float bullRatio) {
  static const uint8_t bayer4x4[4][4] = {
    {  0,  8,  2, 10 },
    { 12,  4, 14,  6 },
    {  3, 11,  1,  9 },
    { 15,  7, 13,  5 }
  };

  int threshold = (int)(bullRatio * 16.0f);
  if (threshold < 0) threshold = 0;
  if (threshold > 16) threshold = 16;

  uint8_t pattern = bayer4x4[py & 0x03][px & 0x03];
  return pattern < threshold;
}

void drawFlowBand(int x, int y, int w, int h) {
  if (histCount < 2) return;
  int centerY = y + h / 2;
  int maxHalfH = (h - 6) / 2;

  struct Layer { float scale; uint8_t tone; };
  Layer layers[3] = { {1.00f,0}, {0.70f,1}, {0.40f,2} };

  for (int l = 0; l < 3; l++) {
    float scale = layers[l].scale;
    uint8_t tone = layers[l].tone;
    for (int i = 0; i < CHART_COLUMNS; i++) {
      if (!historyValid[i]) continue;
      int px = chartXForIndex(i, x, w);
      float frac = flowHistory[i] / FLOW_BAR_MAX_BTCS;
      if (frac < 0) frac = 0;
      if (frac > 1.0f) frac = 1.0f;

      int halfH = (int)(frac * maxHalfH * scale);
      if (halfH < 1) continue;
      int topY = centerY - halfH;
      int bottomY = centerY + halfH;
      if (topY <= centerY && bottomY >= centerY) {
        if (topY == centerY) topY = centerY - 1;
        if (bottomY == centerY) bottomY = centerY + 1;
      }
      float bullRatio = trendBullRatioAt(i);
      uint16_t neutralColor = (tone == 0) ? FLOW_GRAY_OUTER : (tone == 1 ? FLOW_GRAY_MID : FLOW_GRAY_CORE);
      uint16_t greenColor = flowGreenColor(tone);
      uint16_t redColor = flowRedColor(tone);

      for (int yy = topY; yy <= bottomY; yy++) {
        if (yy == centerY) continue;
        float dist = (halfH > 0) ? fabs((float)(yy - centerY)) / (float)halfH : 0.0f;
        float edgeBlend = dist * dist;
        uint16_t pixelColor = flowDitherPickGreen(px, yy, bullRatio) ? greenColor : redColor;
        pixelColor = lerp565(pixelColor, neutralColor, edgeBlend * 0.40f);
        gfx->drawFastHLine(px, yy, CHART_COLUMN_W, pixelColor);
      }
    }
  }
}

// Waterfall
#define WATERFALL_SMOOTH_WINDOW  5
#define WATERFALL_WIDTH_WINDOW   15
#define WATERFALL_TREND_WINDOW   5
#define WATERFALL_NEW_SCALE      2.0f
#define WATERFALL_OLD_SCALE      0.1f
#define WATERFALL_EDGE_NEW_COLOR 0xFFFF
#define WATERFALL_EDGE_OLD_COLOR 0x7BEF
#define WATERFALL_ROAD_GREEN     0x05E0
#define WATERFALL_ROAD_RED       0xA800
#define WATERFALL_ROAD_GRAY      0x52AA
const float WATERFALL_TREND_EPS_PCT = 0.003f;

uint16_t waterfallRoadColorForTrend(int centerIdx, float ageFrac) {
  if (histCount < 3) return scale565(WATERFALL_ROAD_GRAY, 1.0f - ageFrac * 0.5f);

  int halfTrend = WATERFALL_TREND_WINDOW / 2;
  float leftSum = 0.0f;
  float rightSum = 0.0f;
  int leftCount = 0;
  int rightCount = 0;

  for (int k = 1; k <= halfTrend; k++) {
    int li = centerIdx - k;
    int ri = centerIdx + k;
    if (li >= 0) { leftSum += history[li]; leftCount++; }
    if (ri < histCount) { rightSum += history[ri]; rightCount++; }
  }

  if (leftCount == 0 || rightCount == 0) return scale565(WATERFALL_ROAD_GRAY, 1.0f - ageFrac * 0.5f);

  float leftAvg = leftSum / leftCount;
  float rightAvg = rightSum / rightCount;
  float pct = (leftAvg > 0.0f) ? ((rightAvg - leftAvg) / leftAvg) * 100.0f : 0.0f;

  uint16_t baseColor = WATERFALL_ROAD_GRAY;
  if (pct > WATERFALL_TREND_EPS_PCT) baseColor = WATERFALL_ROAD_GREEN;
  else if (pct < -WATERFALL_TREND_EPS_PCT) baseColor = WATERFALL_ROAD_RED;

  return scale565(baseColor, 1.0f - ageFrac * 0.5f);
}

void drawLiveFlowFront(int x, int y, int w, int h) {
  int plotW = plotWidthPx(w);
  int centerX = x + plotW / 2;
  int maxHalfW = (plotW - 2) / 2;
  int frontRows = h;
  if (frontRows < FLOW_LIVE_SLOTS) frontRows = FLOW_LIVE_SLOTS;
  unsigned long newestLiveId = (lastFlowCalc / flowCalcIntervalMs);
  float bullRatio = trendBullRatioAt(newestValidIndex >= 0 ? newestValidIndex : CHART_COLUMNS - 1);
  if (weightedPrice > 0.0f && prevWeightedPrice > 0.0f) {
    if (weightedPrice > prevWeightedPrice) bullRatio = 0.88f;
    else if (weightedPrice < prevWeightedPrice) bullRatio = 0.12f;
  }

  for (int i = 0; i < frontRows; i++) {
    unsigned long sampleId = newestLiveId - (unsigned long)(((long long)i * FLOW_LIVE_SLOTS) / frontRows);
    float sum = 0.0f;
    int count = 0;
    for (int k = -1; k <= 1; k++) {
      unsigned long neighborId = sampleId - k;
      int slot = (int)(neighborId % FLOW_LIVE_SLOTS);
      if (!flowLiveValid[slot] || flowLiveIds[slot] != neighborId) continue;
      sum += flowLiveSamples[slot];
      count++;
    }
    if (count == 0) continue;

    float smoothedFlow = sum / count;
    float flowFrac = smoothedFlow / FLOW_BAR_MAX_BTCS;
    if (flowFrac < 0.0f) flowFrac = 0.0f;
    if (flowFrac > 1.0f) flowFrac = 1.0f;

    float ageFrac = (frontRows > 1) ? ((float)i / (float)(frontRows - 1)) : 0.0f;
    float perspectiveScale = WATERFALL_NEW_SCALE + (WATERFALL_OLD_SCALE - WATERFALL_NEW_SCALE) * ageFrac;
    int rawHalfW = (int)(flowFrac * maxHalfW * perspectiveScale);
    if (rawHalfW < 1) continue;

    int left = centerX - rawHalfW;
    int right = centerX + rawHalfW;
    int clippedLeft = (left < x) ? x : left;
    int clippedRight = (right > x + plotW - 1) ? x + plotW - 1 : right;
    int drawW = clippedRight - clippedLeft + 1;
    if (drawW <= 0) continue;

    int rowY = y + h - 1 - i;
    float fade = 1.0f - ageFrac * 0.45f;
    uint16_t greenColor = scale565(WATERFALL_ROAD_GREEN, fade);
    uint16_t redColor = scale565(WATERFALL_ROAD_RED, fade);
    uint16_t edgeColor = lerp565(WATERFALL_EDGE_NEW_COLOR, WATERFALL_EDGE_OLD_COLOR, ageFrac);

    for (int px = clippedLeft; px <= clippedRight; px++) {
      uint16_t c = flowDitherPickGreen(px, rowY, bullRatio) ? greenColor : redColor;
      gfx->drawPixel(px, rowY, c);
    }
    gfx->drawPixel(clippedLeft, rowY, edgeColor);
    gfx->drawPixel(clippedRight, rowY, edgeColor);
    if (clippedLeft > x) gfx->drawPixel(clippedLeft - 1, rowY, COLOR_BLACK);
    if (clippedRight < x + plotW - 1) gfx->drawPixel(clippedRight + 1, rowY, COLOR_BLACK);
  }
}

void drawFlowWaterfall(int x, int y, int w, int h) {
  if (histCount < 2) return;

  int plotW = plotWidthPx(w);
  int centerX = x + plotW / 2;
  int maxHalfW = (plotW - 2) / 2;
  int halfDetailWindow = WATERFALL_SMOOTH_WINDOW / 2;
  int halfWidthWindow = WATERFALL_WIDTH_WINDOW / 2;

  for (int row = 0; row < h; row++) {
    int rowY = y + h - 1 - row;
    float ageFrac = (h > 1) ? ((float)row / (float)(h - 1)) : 1.0f;

    int centerIdx;
    if (CHART_COLUMNS >= h) centerIdx = CHART_COLUMNS - 1 - row;
    else centerIdx = (int)(((float)(h - 1 - row) / (float)(h - 1)) * (CHART_COLUMNS - 1));

    if (centerIdx < 0 || centerIdx >= CHART_COLUMNS || !historyValid[centerIdx]) continue;

    float detailSum = 0;
    int detailSamples = 0;
    for (int k = -halfDetailWindow; k <= halfDetailWindow; k++) {
      int idx = centerIdx + k;
      if (idx >= 0 && idx < CHART_COLUMNS && historyValid[idx]) {
        detailSum += flowHistory[idx];
        detailSamples++;
      }
    }
    if (detailSamples == 0) continue;
    float detailFlow = detailSum / detailSamples;

    float widthSum = 0;
    int widthSamples = 0;
    for (int k = -halfWidthWindow; k <= halfWidthWindow; k++) {
      int idx = centerIdx + k;
      if (idx >= 0 && idx < CHART_COLUMNS && historyValid[idx]) {
        widthSum += flowHistory[idx];
        widthSamples++;
      }
    }
    if (widthSamples == 0) continue;
    float widthFlow = widthSum / widthSamples;

    float flowFrac = widthFlow / FLOW_BAR_MAX_BTCS;
    if (flowFrac < 0) flowFrac = 0;

    float perspectiveScale = WATERFALL_NEW_SCALE + (WATERFALL_OLD_SCALE - WATERFALL_NEW_SCALE) * ageFrac;
    float finalFrac = flowFrac * perspectiveScale;
    int rawHalfW = (int)(finalFrac * maxHalfW);
    if (rawHalfW < 1) continue;

    int left = centerX - rawHalfW;
    int right = centerX + rawHalfW;
    int clippedLeft = (left < x) ? x : left;
    int clippedRight = (right > x + plotW - 1) ? x + plotW - 1 : right;
    int drawW = clippedRight - clippedLeft + 1;
    if (drawW <= 0) continue;

    uint16_t roadColor = waterfallRoadColorForTrend(centerIdx, ageFrac);

    float detailFrac = detailFlow / FLOW_BAR_MAX_BTCS;
    if (detailFrac < 0.10f) {
      roadColor = lerp565(roadColor, scale565(WATERFALL_ROAD_GRAY, 1.0f - ageFrac * 0.5f), 0.45f);
    }

    gfx->drawFastHLine(clippedLeft, rowY, drawW, roadColor);

    uint16_t edgeColor = lerp565(WATERFALL_EDGE_NEW_COLOR, WATERFALL_EDGE_OLD_COLOR, ageFrac);
    gfx->drawPixel(clippedLeft, rowY, edgeColor);
    gfx->drawPixel(clippedRight, rowY, edgeColor);
  }
}

uint16_t priceLineColor(float prevP, float currP) {
  if (prevP <= 0.0f) return COLOR_GRAY;
  if (currP > prevP) return COLOR_PRICE_UP;
  if (currP < prevP) return COLOR_PRICE_DOWN;
  return COLOR_GRAY;
}

void drawChartCenterMessage(int x, int y, int w, int h) {
  if (histCount >= 2) return;
  gfx->setTextSize(1);
  gfx->setTextColor(COLOR_LIGHTGRAY);
  gfx->setCursor(x + TEXT_MARGIN_X, y + h / 2 - 4);
  gfx->print("Esperando datos...");
}

void drawTrendBadgeInChart(int x, int y, int w) {
  const char* trendLabel = "-";
  uint16_t trendColor = COLOR_LIGHTGRAY;

  if (currentTrendSignal == SIGNAL_LONG) {
    trendLabel = "L";
    trendColor = COLOR_GREEN;
  } else if (currentTrendSignal == SIGNAL_SHORT) {
    trendLabel = "S";
    trendColor = COLOR_SMA_SLOW;
  }

  const int badgeW = 22;
  const int badgeH = 26;
  int badgeX = x + TEXT_MARGIN_X - 4;
  int badgeY = y + 110;

  gfx->fillRect(badgeX, badgeY, badgeW, badgeH, COLOR_BLACK);
  gfx->drawRect(badgeX, badgeY, badgeW, badgeH, COLOR_DARKGRAY);
  gfx->setTextSize(3);
  gfx->setTextColor(trendColor);
  gfx->setCursor(badgeX + 3, badgeY + 2);
  gfx->print(trendLabel);
}

void drawPriceLineOnly(int x, int y, int w, int h) {
  if (histCount < 2) return;

  float minP, maxP;
  computeChartRange(&minP, &maxP);

  int prevX = -1, prevY = -1;
  for (int i = 0; i < CHART_COLUMNS; i++) {
    if (!historyValid[i]) {
      prevX = -1;
      prevY = -1;
      continue;
    }
    int px = chartXForIndex(i, x, w);
    int py = chartYForPrice(history[i], minP, maxP, y, h);

    if (prevX >= 0) {
      uint16_t c = priceLineColor(history[i - 1], history[i]);
      drawPriceStepLink(prevX, prevY, px, py, c);
      drawPricePoint2px(px, py, c);
    } else {
      drawPricePoint2px(px, py, COLOR_WHITE);
    }
    prevX = px;
    prevY = py;
  }
}

void drawSMAForPeriod(int x, int y, int w, int h, int period, uint16_t color) {
  if (histCount < 2) return;

  float minP, maxP;
  computeChartRange(&minP, &maxP);
  uint16_t darkColor = (period == SMA_FAST_PERIOD) ? COLOR_SMA_FAST_DARK : COLOR_SMA_SLOW_DARK;

  int prevX = -1, prevY = -1;
  for (int i = 0; i < CHART_COLUMNS; i++) {
    float avg = 0.0f;
    if (!calcSMAAt(i, period, &avg)) {
      prevX = -1;
      prevY = -1;
      continue;
    }

    int px = chartXForIndex(i, x, w);
    int py = clampChartInnerY(chartYForPrice(avg, minP, maxP, y, h), y, h);

    if (prevX >= 0) {
      drawDualStrokeLine(prevX, prevY, px, py, color, darkColor);
    }
    drawDualStrokePoint(px, py, CHART_COLUMN_W, color, darkColor);
    prevX = px;
    prevY = py;
  }
}

void drawTailRegion(int x, int y, int w, int h) {
  int lastIdx = CHART_COLUMNS - 1;
  int prevIdx = CHART_COLUMNS - 2;
  int prevX = chartXForIndex(prevIdx >= 0 ? prevIdx : 0, x, w);
  int currX = chartXForIndex(lastIdx, x, w);
  int xStart = prevX + 8;
  int xEnd = currX + CHART_COLUMN_W - 1;
  if (xStart < x + 1) xStart = x + 1;
  if (xEnd < xStart) return;

  gfx->fillRect(xStart, y + 1, xEnd - xStart + 1, h - 2, COLOR_BLACK);

  float minP, maxP;
  computeChartRange(&minP, &maxP);

  if (historyValid[lastIdx] && prevIdx >= 0 && historyValid[prevIdx]) {
      int prevY = chartYForPrice(history[prevIdx], minP, maxP, y, h);
      int currY = chartYForPrice(history[lastIdx], minP, maxP, y, h);
      uint16_t tailColor = priceLineColor(history[prevIdx], history[lastIdx]);
      drawPriceStepLink(prevX, prevY, currX, currY, tailColor);
      drawPricePoint2px(currX, currY, tailColor);

    float prevSma = 0.0f;
    float currSma = 0.0f;
    if (calcSMAAt(prevIdx, SMA_SLOW_PERIOD, &prevSma) && calcSMAAt(lastIdx, SMA_SLOW_PERIOD, &currSma)) {
      int smaPrevY = chartYForPrice(prevSma, minP, maxP, y, h);
      int smaCurrY = chartYForPrice(currSma, minP, maxP, y, h);
      smaPrevY = clampChartInnerY(smaPrevY, y, h);
      smaCurrY = clampChartInnerY(smaCurrY, y, h);
      drawDualStrokeLine(prevX, smaPrevY, currX, smaCurrY, COLOR_SMA_SLOW, COLOR_SMA_SLOW_DARK);
      drawDualStrokePoint(currX, smaCurrY, CHART_COLUMN_W, COLOR_SMA_SLOW, COLOR_SMA_SLOW_DARK);
    }
    if (calcSMAAt(prevIdx, SMA_FAST_PERIOD, &prevSma) && calcSMAAt(lastIdx, SMA_FAST_PERIOD, &currSma)) {
      int smaPrevY = chartYForPrice(prevSma, minP, maxP, y, h);
      int smaCurrY = chartYForPrice(currSma, minP, maxP, y, h);
      smaPrevY = clampChartInnerY(smaPrevY, y, h);
      smaCurrY = clampChartInnerY(smaCurrY, y, h);
      drawDualStrokeLine(prevX, smaPrevY, currX, smaCurrY, COLOR_SMA_FAST, COLOR_SMA_FAST_DARK);
      drawDualStrokePoint(currX, smaCurrY, CHART_COLUMN_W, COLOR_SMA_FAST, COLOR_SMA_FAST_DARK);
    }
  }

  drawTrendBadgeInChart(x, y, w);
  chartTailDirty = false;
}

void drawChart(int x, int y, int w, int h) {
  if (histCount < 2) {
    drawChartCenterMessage(x, y, w, h);
    drawTrendBadgeInChart(x, y, w);
    return;
  }
  drawPriceLineOnly(x, y, w, h);
  drawSMAForPeriod(x, y, w, h, SMA_SLOW_PERIOD, COLOR_SMA_SLOW);
  drawSMAForPeriod(x, y, w, h, SMA_FAST_PERIOD, COLOR_SMA_FAST);
  drawTrendBadgeInChart(x, y, w);
}

void drawPriceScaleTicks(int x, int y, int h, float minP, float maxP) {
  if (maxP <= minP) return;

  int firstTick = (int)(ceilf(minP / 10.0f) * 10.0f);
  int lastTick = (int)(floorf(maxP / 10.0f) * 10.0f);

  for (int tick = firstTick; tick <= lastTick; tick += 10) {
    int tickY = chartYForPrice((float)tick, minP, maxP, y, h);
    if (tickY < y + 2 || tickY > y + h - 3) continue;

    int tickW = 0;
    int tickH = 1;
    uint16_t tickColor = COLOR_DARKGRAY;

    if ((tick % 1000) == 0) {
      tickW = PRICE_MARKER_W;
      tickH = 3;
      tickColor = COLOR_WHITE;
    } else if ((tick % 100) == 0) {
      tickW = PRICE_MARKER_W - 2;
      tickH = 3;
      tickColor = COLOR_LIGHTGRAY;
    } else if ((tick % 10) == 0) {
      tickW = 3;
      tickH = 1;
      tickColor = COLOR_ORANGE;
    }

    if (tickW <= 0) continue;

    int topY = tickY - (tickH / 2);
    for (int yy = 0; yy < tickH; yy++) {
      gfx->drawFastHLine(x, topY + yy, tickW, tickColor);
    }
  }
}

void drawLivePriceTail(int x, int y, int w, int h) {
  if (newestValidIndex < 0 || weightedPrice <= 0 || !hasFreshWeightedPrice()) return;

  float minP, maxP;
  computeChartRange(&minP, &maxP);

  int lastIdx = newestValidIndex;
  int xLast = chartXForIndex(lastIdx, x, w);
  int yHist = chartYForPrice(history[lastIdx], minP, maxP, y, h);
  int yLive = chartYForPrice(weightedPrice, minP, maxP, y, h);
  if (yLive < y + 1) yLive = y + 1;
  if (yLive > y + h - 2) yLive = y + h - 2;

  int markerX = PRICE_MARKER_X;
  gfx->fillRect(markerX, y + 1, PRICE_MARKER_W, h - 2, COLOR_BLACK);
  drawPriceScaleTicks(markerX, y, h, minP, maxP);
  gfx->drawFastHLine(markerX, yLive, PRICE_MARKER_W, COLOR_WHITE);

  if (yLive == yHist) return;

  int top = (yLive < yHist) ? yLive : yHist;
  int height = abs(yLive - yHist) + 1;

  uint16_t c = priceLineColor(history[lastIdx], weightedPrice);
  gfx->drawFastVLine(xLast, top, height, c);
}

void formatPriceCompact(float value, char* outBuf, size_t bufSize) {
  formatAR(value, 0, outBuf, bufSize);
}

void drawHeader() {
  gfx->fillRect(0, 0, 320, 30, COLOR_BLACK);

  int row1Y = 1;
  int row2Y = 20;
  uint16_t btcColor = btcLabelColorFromRange();
  uint16_t priceColor = COLOR_WHITE;
  if (weightedPrice > 0 && prevWeightedPrice > 0) {
    if (weightedPrice > prevWeightedPrice) priceColor = COLOR_GREEN;
    else if (weightedPrice < prevWeightedPrice) priceColor = COLOR_RED;
  }

  gfx->setTextSize(2);
  gfx->setTextColor(btcColor);
  gfx->setCursor(TEXT_MARGIN_X, row1Y);
  gfx->print("BTC");

  int xPrice = TEXT_MARGIN_X + (3 * 12) + 8;
  if (weightedPrice > 0) {
    char priceBuf[24];
    formatAR(weightedPrice, 2, priceBuf, sizeof(priceBuf));

    char priceStr[28];
    snprintf(priceStr, sizeof(priceStr), "$%s", priceBuf);

    gfx->setTextColor(priceColor);
    gfx->setCursor(xPrice, row1Y);
    gfx->print(priceStr);
  } else {
    gfx->setTextColor(COLOR_WHITE);
    gfx->setCursor(xPrice, row1Y);
    gfx->print("---");
  }

  char flowNum[16];
  formatAR(totalFlowBTCs, 2, flowNum, sizeof(flowNum));

  char flowStr[24];
  snprintf(flowStr, sizeof(flowStr), "BTC/S:%s", flowNum);

  int flowW = textWidthPx(flowStr, 2);
  int xFlow = 320 - flowW - TEXT_MARGIN_X;

  gfx->setTextSize(2);
  gfx->setTextColor(COLOR_CYAN);
  gfx->setCursor(xFlow, row1Y);
  gfx->print(flowStr);

  int x2 = TEXT_MARGIN_X;

  int firstValidIdx = firstValidHistoryIndex();
  if (weightedPrice > 0 && firstValidIdx >= 0 && history[firstValidIdx] > 0) {
    float base = history[firstValidIdx];
    float pct = ((weightedPrice - base) / base) * 100.0f;

    uint16_t pctColor = COLOR_GRAY;
    if (pct > 0) pctColor = COLOR_GREEN;
    else if (pct < 0) pctColor = COLOR_RED;

    char pctNum[16];
    formatAR(fabs(pct), 2, pctNum, sizeof(pctNum));

    char pctStr[20];
    if (pct >= 0) snprintf(pctStr, sizeof(pctStr), "+%s%%", pctNum);
    else snprintf(pctStr, sizeof(pctStr), "-%s%%", pctNum);

    gfx->setTextSize(1);
    gfx->setTextColor(pctColor);
    gfx->setCursor(x2, row2Y);
    gfx->print(pctStr);

    x2 += textWidthPx(pctStr, 1) + 12;
  }

  if (histCount > 0) {
    float minP, maxP;
    computeChartRange(&minP, &maxP);

    char maxBuf[16];
    formatPriceCompact(maxP, maxBuf, sizeof(maxBuf));
    char hStr[24];
    snprintf(hStr, sizeof(hStr), "H:%s", maxBuf);

    gfx->setTextSize(1);
    gfx->setTextColor(COLOR_GREEN);
    gfx->setCursor(x2, row2Y);
    gfx->print(hStr);

    x2 += textWidthPx(hStr, 1) + 10;

    char minBuf[16];
    formatPriceCompact(minP, minBuf, sizeof(minBuf));
    char lStr[24];
    snprintf(lStr, sizeof(lStr), "L:%s", minBuf);

    gfx->setTextColor(COLOR_RED);
    gfx->setCursor(x2, row2Y);
    gfx->print(lStr);
  }

  if (histCount > 0) {
    unsigned long totalMs = CHART_WINDOW_MS;
    unsigned long totalSec = totalMs / 1000UL;
    unsigned long mm = totalSec / 60UL;
    unsigned long ss = totalSec % 60UL;

    char winStr[16];
    snprintf(winStr, sizeof(winStr), "%lu:%02lu", mm, ss);

    int winW = textWidthPx(winStr, 1);
    int xWin = 320 - winW - TEXT_MARGIN_X;

    gfx->setTextSize(1);
    gfx->setTextColor(COLOR_LIGHTGRAY);
    gfx->setCursor(xWin, row2Y);
    gfx->print(winStr);
  }
}

void drawChartArea() {
  gfx->fillRect(CHART_INNER_X, CHART_INNER_Y, CHART_INNER_W, CHART_INNER_H, COLOR_BLACK);
  drawChart(CHART_INNER_X, CHART_INNER_Y, CHART_INNER_W, CHART_INNER_H);
  gfx->fillRect(PRICE_MARKER_X, CHART_INNER_Y, PRICE_MARKER_W, CHART_INNER_H, COLOR_BLACK);
  chartDirty = false;
}

// -------------------- WiFi --------------------
bool tryConnectWifi(int idx, unsigned long timeoutMs) {
  if (idx < 0 || idx >= wifiCount) return false;

  statusText = "Conectando WiFi";
  detailText = wifiList[idx].ssid;
  drawHeader();
  drawChartArea();

  Serial.printf("[WiFi] Probando %s...\n", wifiList[idx].ssid);

  WiFi.disconnect(true, true);
  delay(300);
  WiFi.begin(wifiList[idx].ssid, wifiList[idx].pass);

  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) delay(300);

  if (WiFi.status() == WL_CONNECTED) {
    currentWifiIdx = idx;
    statusText = "WiFi OK";
    detailText = String(wifiList[idx].ssid) + " " + WiFi.localIP().toString();
    Serial.printf("[WiFi] Conectado a %s, IP: %s\n", wifiList[idx].ssid, WiFi.localIP().toString().c_str());
    drawHeader();
    return true;
  }

  Serial.printf("[WiFi] Fallo con %s\n", wifiList[idx].ssid);
  return false;
}

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  if (currentWifiIdx >= 0) {
    if (tryConnectWifi(currentWifiIdx, 15000)) return;
  }

  for (int i = 0; i < wifiCount; i++) {
    if (i == currentWifiIdx) continue;
    if (tryConnectWifi(i, 12000)) return;
  }

  currentWifiIdx = -1;
  statusText = "Sin WiFi";
  detailText = "Ninguna red conecta";
  Serial.println("[WiFi] FAIL: ninguna red conecta");
  drawHeader();
}

// -------------------- WS events --------------------
void onBinanceEvent(WStype_t type, uint8_t * payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      binance.wsConnected = false;
      Serial.println("[BIN] Desconectado");
      break;

    case WStype_CONNECTED:
      binance.wsConnected = true;
      Serial.printf("[BIN] Conectado a: %s\n", (char*)payload);
      break;

      case WStype_TEXT: {
        String msg = String((char*)payload);
        JsonDocument doc;
        if (!deserializeJson(doc, msg)) {
          const char* p = doc["p"];
          const char* q = doc["q"];
          if (p && strlen(p) > 0) {
            binance.livePrice = atof(p);
            binance.priceOk = (binance.livePrice > 0);
            binance.lastPriceMs = millis();
        }

          if (q && strlen(q) > 0) {
            float btc = atof(q);
            if (btc > 0) binance.btcAccum += btc;
            if (btc > 0) {
              recomputeWeightedPrice();
              ingestChartSample(weightedPrice, totalFlowBTCs);
            } else {
              recomputeWeightedPrice();
            }
          } else {
            recomputeWeightedPrice();
        }
      }
      break;
    }

    case WStype_ERROR:
      binance.wsConnected = false;
      Serial.println("[BIN] Error");
      break;

    default:
      break;
  }
}

void onCoinbaseEvent(WStype_t type, uint8_t * payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      coinbase.wsConnected = false;
      Serial.println("[CB] Desconectado");
      break;

    case WStype_CONNECTED: {
      coinbase.wsConnected = true;
      Serial.printf("[CB] Conectado a: %s\n", (char*)payload);

      StaticJsonDocument<256> doc;
      doc["type"] = "subscribe";
      JsonArray channels = doc.createNestedArray("channels");
      JsonObject ch = channels.createNestedObject();
      ch["name"] = "ticker";
      JsonArray products = ch.createNestedArray("product_ids");
      products.add("BTC-USD");

      String out;
      serializeJson(doc, out);
      wsCoinbase.sendTXT(out);
      break;
    }

    case WStype_TEXT: {
      String msg = String((char*)payload);
        JsonDocument doc;
        if (!deserializeJson(doc, msg)) {
          const char* typeStr = doc["type"];
          if (typeStr && String(typeStr) == "ticker") {
            const char* p = doc["price"];
            const char* s = doc["last_size"];

            if (p && strlen(p) > 0) {
              coinbase.livePrice = atof(p);
              coinbase.priceOk = (coinbase.livePrice > 0);
              coinbase.lastPriceMs = millis();
          }

            if (s && strlen(s) > 0) {
              float btc = atof(s);
              if (btc > 0) coinbase.btcAccum += btc;
              if (btc > 0) {
                recomputeWeightedPrice();
                ingestChartSample(weightedPrice, totalFlowBTCs);
              } else {
                recomputeWeightedPrice();
              }
            } else {
              recomputeWeightedPrice();
          }
        }
      }
      break;
    }

    case WStype_ERROR:
      coinbase.wsConnected = false;
      Serial.println("[CB] Error");
      break;

    default:
      break;
  }
}

void connectBinanceWS() {
  wsBinance.beginSSL("data-stream.binance.vision", 443, "/ws/btcusdt@trade");
  wsBinance.onEvent(onBinanceEvent);
  wsBinance.setReconnectInterval(5000);
  wsBinance.enableHeartbeat(15000, 3000, 2);
}

void connectCoinbaseWS() {
  wsCoinbase.beginSSL("ws-feed.exchange.coinbase.com", 443, "/");
  wsCoinbase.onEvent(onCoinbaseEvent);
  wsCoinbase.setReconnectInterval(5000);
  wsCoinbase.enableHeartbeat(15000, 3000, 2);
}

// -------------------- Setup / Loop --------------------
void setup() {
  setCpuFrequencyMhz(80);
  Serial.begin(115200);
  delay(500);

  ledcAttach(BL_PIN, 5000, 8);
  ledcWrite(BL_PIN, 140);

  led.begin();
  led.setBrightness(255);
  led.setPixelColor(0, led.Color(0, 0, 0));
  led.show();

  gfx->begin();
  gfx->fillScreen(COLOR_BLACK);
  gfx->drawRect(CHART_BOX_X, CHART_BOX_Y, CHART_BOX_W, CHART_BOX_H, COLOR_DARKGRAY);

  for (int i = 0; i < CHART_COLUMNS; i++) {
    history[i] = 0;
    flowHistory[i] = 0;
    historyValid[i] = false;
    historyBucketIds[i] = 0;
  }
  for (int i = 0; i < BUCKET_STORAGE_COLUMNS; i++) {
    bucketPrice[i] = 0;
    bucketFlow[i] = 0;
    bucketValid[i] = false;
    bucketIds[i] = 0;
  }
  for (int i = 0; i < FLOW_SECONDS_SLOTS; i++) {
    flowSecondBuckets[i] = 0;
    flowSecondValid[i] = false;
    flowSecondIds[i] = 0;
  }
  for (int i = 0; i < FLOW_LIVE_SLOTS; i++) {
    flowLiveSamples[i] = 0;
    flowLiveValid[i] = false;
    flowLiveIds[i] = 0;
  }

  rebuildVisibleHistory();

  drawHeader();
  drawChartArea();
  connectWiFi();

  if (WiFi.status() == WL_CONNECTED) {
    connectBinanceWS();
    connectCoinbaseWS();
    lastFlowCalc = millis();
  }

  drawHeader();
  drawChartArea();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
    if (WiFi.status() == WL_CONNECTED) {
      connectBinanceWS();
      connectCoinbaseWS();
      lastFlowCalc = millis();
    }
  }

  wsBinance.loop();
  wsCoinbase.loop();
  updateFlows();

  static unsigned long lastLedUpdate = 0;
  unsigned long nowMs = millis();
  advanceChartTimeIfNeeded();

  if (nowMs - lastLedUpdate >= 50) {
    lastLedUpdate = nowMs;
    updateLed();
  }

  if (nowMs - lastDraw >= drawIntervalMs) {
    lastDraw = nowMs;
    drawHeader();
  }

  if (chartDirty) {
    drawChartArea();
  }

  if (!chartDirty && chartTailDirty) {
    drawTailRegion(CHART_INNER_X, CHART_INNER_Y, CHART_INNER_W, CHART_INNER_H);
  }

  if (!chartDirty && (nowMs - lastLiveTailDraw >= liveTailIntervalMs)) {
    lastLiveTailDraw = nowMs;
    drawLivePriceTail(CHART_INNER_X, CHART_INNER_Y, CHART_INNER_W, CHART_INNER_H);
  }

  delay(10);
}
