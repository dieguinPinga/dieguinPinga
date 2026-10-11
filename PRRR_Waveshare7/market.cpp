// Modelo de mercado PRRR. Funciones y nombres tomados del sketch original;
// los cambios respecto del original estan comentados con "(7in)".
#include "market.h"
#include "color565.h"
#include <esp_heap_caps.h>

// -------------------- Estado --------------------
ExchangeData binance  = { "BIN", COLOR_BINANCE,  -1, 0.5f, false, false, 0, 0, 0, 0 };
ExchangeData coinbase = { "CB",  COLOR_COINBASE, -1, 0.5f, false, false, 0, 0, 0, 0 };

double weightedPrice = -1.0;
double prevWeightedPrice = -1.0;
double firstWeightedPrice = 0.0;
bool hasFirstWeighted = false;

String statusText = "Boot...";
String detailText = "";

CrossSignal currentTrendSignal = SIGNAL_FLAT;

int currentWindowIndex = DEFAULT_WINDOW_INDEX;
unsigned long chartWindowMs = WINDOW_PRESETS[DEFAULT_WINDOW_INDEX].priceWindowMs;
unsigned long flowWindowSeconds = WINDOW_PRESETS[DEFAULT_WINDOW_INDEX].flowWindowSeconds;
unsigned long bucketMs = WINDOW_PRESETS[DEFAULT_WINDOW_INDEX].priceWindowMs / CHART_COLUMNS;

double history[CHART_COLUMNS];
float flowHistory[CHART_COLUMNS];
bool historyValid[CHART_COLUMNS];
unsigned long historyBucketIds[CHART_COLUMNS];

static double bucketPrice[BUCKET_STORAGE_COLUMNS];
static float bucketFlow[BUCKET_STORAGE_COLUMNS];
static bool bucketValid[BUCKET_STORAGE_COLUMNS];
static unsigned long bucketIds[BUCKET_STORAGE_COLUMNS];

// (7in) Arreglos grandes en PSRAM (en el original eran globales en SRAM).
static float* flowSecondBuckets = nullptr;
static bool* flowSecondValid = nullptr;
static unsigned long* flowSecondIds = nullptr;

// (7in) Precio por segundo: solo se usa para reconstruir el grafico al cambiar de ventana.
static double* priceSecondValue = nullptr;
static bool* priceSecondValid = nullptr;
static unsigned long* priceSecondIds = nullptr;

float flowLiveSamples[FLOW_LIVE_SLOTS];
bool flowLiveValid[FLOW_LIVE_SLOTS];
unsigned long flowLiveIds[FLOW_LIVE_SLOTS];

int histCount = 0;
int newestValidIndex = -1;

unsigned long lastFlowCalc = 0;
float totalFlowBTCs = 0;

bool chartDirty = true;
bool chartTailDirty = false;
bool flowDirty = true;

static unsigned long lastVisibleNewestBucketId = 0;
static unsigned long chartLogicalBucketId = 0;
static unsigned long lastBucketAdvanceMs = 0;

float ledSmoothedPos = 0.5f;
uint8_t ledR = 0, ledG = 0, ledB = 0;

static float binBuyAccum = 0.0f;
static float binSellAccum = 0.0f;
float aggressorBuyBTCs = 0.0f;
float aggressorSellBTCs = 0.0f;

ChartDiag chartDiag = {};

uint32_t tradeCountBin = 0;
uint32_t tradeCountCb = 0;

// -------------------- Helpers --------------------
bool isFresh(const ExchangeData& ex) {
  return ex.priceOk && (millis() - ex.lastPriceMs < stalePriceMs);
}

bool hasFreshWeightedPrice() {
  return weightedPrice > 0.0 && (isFresh(binance) || isFresh(coinbase));
}

static unsigned long currentChartBucketId() {
  return chartLogicalBucketId;
}

int firstValidHistoryIndex() {
  for (int i = 0; i < CHART_COLUMNS; i++) {
    if (historyValid[i]) return i;
  }
  return -1;
}

void computeChartRange(double* outMin, double* outMax) {
  if (histCount <= 0) {
    *outMin = 0.0;
    *outMax = 1.0;
    return;
  }

  bool found = false;
  double minP = 0.0;
  double maxP = 0.0;
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
    *outMin = 0.0;
    *outMax = 1.0;
    return;
  }
  if (weightedPrice > 0) {
    if (weightedPrice < minP) minP = weightedPrice;
    if (weightedPrice > maxP) maxP = weightedPrice;
  }
  if ((maxP - minP) < 0.0001) maxP = minP + 1.0;
  double pad = (maxP - minP) * CHART_RANGE_PAD_PCT;
  if (pad < 1.0) pad = 1.0;
  minP -= pad;
  maxP += pad;
  *outMin = minP;
  *outMax = maxP;
}

// (7in) Maximo/minimo reales de la ventana visible. El original mostraba en H:/L:
// el rango del grafico, que incluye un 8 % de margen (CHART_RANGE_PAD_PCT).
bool computeVisibleHighLow(double* outLow, double* outHigh) {
  bool found = false;
  double lo = 0.0, hi = 0.0;
  for (int i = 0; i < CHART_COLUMNS; i++) {
    if (!historyValid[i]) continue;
    if (!found) { lo = hi = history[i]; found = true; continue; }
    if (history[i] < lo) lo = history[i];
    if (history[i] > hi) hi = history[i];
  }
  if (!found) return false;
  if (weightedPrice > 0) {
    if (weightedPrice < lo) lo = weightedPrice;
    if (weightedPrice > hi) hi = weightedPrice;
  }
  *outLow = lo;
  *outHigh = hi;
  return true;
}

static bool chartRangeChanged(double oldMin, double oldMax, double newMin, double newMax) {
  double oldSpan = oldMax - oldMin;
  double newSpan = newMax - newMin;
  if (oldSpan < 0.0001) oldSpan = 1.0;
  if (newSpan < 0.0001) newSpan = 1.0;

  double minShift = fabs(newMin - oldMin);
  double maxShift = fabs(newMax - oldMax);

  return (minShift > (oldSpan * 0.02)) || (maxShift > (oldSpan * 0.02)) || (fabs(newSpan - oldSpan) > (oldSpan * 0.03));
}

// (7in) Suma en double: 55 precios de ~1e5 suman ~6e6, donde un float ya pierde ~0,5 USD.
bool calcSMAAt(int idx, int period, double* outAvg) {
  if (idx < 0 || idx >= CHART_COLUMNS || !historyValid[idx]) return false;
  double sum = 0.0;
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

static void updateCrossSignal() {
  currentTrendSignal = SIGNAL_FLAT;

  double latestFast = 0.0;
  double latestSlow = 0.0;
  if (calcSMAAt(newestValidIndex, SMA_FAST_PERIOD, &latestFast) && calcSMAAt(newestValidIndex, SMA_SLOW_PERIOD, &latestSlow)) {
    if (latestFast > latestSlow) currentTrendSignal = SIGNAL_LONG;
    else if (latestFast < latestSlow) currentTrendSignal = SIGNAL_SHORT;
  }
}

static void rebuildFlowHistoryFromSeconds() {
  const unsigned long windowSec = flowWindowSeconds;
  unsigned long newestSecond = millis() / 1000UL;
  // (7in) El original hacia "if (oldestSecond < 0) oldestSecond = 0": mientras el
  // uptime era menor que la ventana, el rio se dibujaba desde la izquierda y no quedaba
  // alineado con el precio (que crece desde la derecha). Ahora los segundos negativos
  // simplemente no tienen datos.
  long long oldestSecond = (long long)newestSecond - (long long)windowSec + 1LL;

  for (int i = 0; i < CHART_COLUMNS; i++) {
    long long segStartS = oldestSecond + ((long long)i * (long long)windowSec) / CHART_COLUMNS;
    long long segEndS = oldestSecond + (((long long)(i + 1) * (long long)windowSec) / CHART_COLUMNS);
    if (segEndS <= segStartS) segEndS = segStartS + 1;
    if (segEndS <= 0) {
      flowHistory[i] = 0.0f;
      continue;
    }
    unsigned long segStart = (unsigned long)(segStartS < 0 ? 0 : segStartS);
    unsigned long segEnd = (unsigned long)segEndS;

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

// (7in) Ya no reconstruye el rio de flujo: flowHistory depende solo del reloj y de
// flowSecondBuckets, que cambian en updateFlows(). El original lo recalculaba en cada
// trade (hasta 14400 iteraciones por trade); ahora se hace cada 120 ms con identico resultado.
static void rebuildVisibleHistory() {
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
      history[i] = 0.0;
      historyValid[i] = false;
      historyBucketIds[i] = 0;
    }
  }

  updateCrossSignal();
}

static void clearPriceBuckets() {
  for (int i = 0; i < CHART_COLUMNS; i++) {
    history[i] = 0.0;
    flowHistory[i] = 0.0f;
    historyValid[i] = false;
    historyBucketIds[i] = 0;
  }
  for (int i = 0; i < BUCKET_STORAGE_COLUMNS; i++) {
    bucketPrice[i] = 0.0;
    bucketFlow[i] = 0.0f;
    bucketValid[i] = false;
    bucketIds[i] = 0;
  }
  histCount = 0;
  newestValidIndex = -1;
  chartLogicalBucketId = 0;
  lastVisibleNewestBucketId = 0;
  lastBucketAdvanceMs = 0;
}

static void writeBucketSample(unsigned long bucketId, double price, float flowDelta, bool accumulateFlow) {
  if (price <= 0.0) return;

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

static void recordPriceSecond(double price) {
  unsigned long sec = millis() / 1000UL;
  int slot = (int)(sec % PRICE_SECONDS_SLOTS);
  priceSecondValue[slot] = price;
  priceSecondValid[slot] = true;
  priceSecondIds[slot] = sec;
}

static void ingestChartSample(double price, float flowDelta) {
  if (price <= 0.0) return;
  unsigned long nowMs = millis();
  if (lastBucketAdvanceMs == 0) {
    lastBucketAdvanceMs = nowMs;
  } else if ((nowMs - lastBucketAdvanceMs) >= bucketMs) {
    chartLogicalBucketId++;
    lastBucketAdvanceMs = nowMs;
  }

  unsigned long targetBucketId = currentChartBucketId();
  bool bucketAdvanced = (targetBucketId > lastVisibleNewestBucketId);

  double oldMin = 0.0;
  double oldMax = 1.0;
  computeChartRange(&oldMin, &oldMax);

  writeBucketSample(targetBucketId, price, flowDelta, true);
  recordPriceSecond(price);

  if (!hasFirstWeighted) {
    firstWeightedPrice = price;
    hasFirstWeighted = true;
  }

  rebuildVisibleHistory();
  double newMin = 0.0;
  double newMax = 1.0;
  computeChartRange(&newMin, &newMax);

  // Diagnostico: mismas comparaciones que el original, solo se cuentan
  bool rangeChangedDiag = chartRangeChanged(oldMin, oldMax, newMin, newMax);
  if (bucketAdvanced) chartDiag.bucketAdvances++;
  if (rangeChangedDiag) {
    chartDiag.rescales++;
    chartDiag.lastOldMin = oldMin;
    chartDiag.lastOldMax = oldMax;
    chartDiag.lastNewMin = newMin;
    chartDiag.lastNewMax = newMax;
    chartDiag.lastRescaleMs = nowMs;
  } else if (!bucketAdvanced && (newMin != oldMin || newMax != oldMax)) {
    chartDiag.rangeDrifts++;
  }

  bool needFullRedraw = bucketAdvanced || chartRangeChanged(oldMin, oldMax, newMin, newMax);
  // (7in) Un redibujado completo pendiente no se cancela con un trade posterior.
  if (needFullRedraw) chartDirty = true;
  else chartTailDirty = true;
}

// -------------------- Ventanas --------------------
// (7in) Nuevo: cambio de ventana desde el tactil. Las ventanas del original eran
// constantes de compilacion; aca se reconstruyen los buckets de precio desde el
// historial por segundo, para no perder lo ya recibido. Los buckets reconstruidos
// usan tiempo de reloj (un hueco sin datos queda como columna vacia); los nuevos
// siguen la logica original (el id avanza solo cuando llega un trade).
void marketApplyWindow(int idx) {
  if (idx < 0 || idx >= WINDOW_PRESET_COUNT) return;
  currentWindowIndex = idx;
  chartWindowMs = WINDOW_PRESETS[idx].priceWindowMs;
  flowWindowSeconds = WINDOW_PRESETS[idx].flowWindowSeconds;
  bucketMs = chartWindowMs / CHART_COLUMNS;
  if (bucketMs < 1) bucketMs = 1;

  clearPriceBuckets();

  const unsigned long baseId = 1000000UL;
  unsigned long nowMs = millis();
  unsigned long nowSec = nowMs / 1000UL;
  bool any = false;

  for (int b = 0; b < BUCKET_STORAGE_COLUMNS; b++) {
    unsigned long long ageStartMs = (unsigned long long)b * bucketMs;
    unsigned long long ageEndMs = (unsigned long long)(b + 1) * bucketMs;
    if (ageStartMs > nowMs) break;
    long long newestSec = (long long)(nowMs - ageStartMs) / 1000LL;
    long long oldestSec = (ageEndMs >= nowMs) ? -1LL : (long long)(nowMs - ageEndMs) / 1000LL;
    if ((long long)nowSec - oldestSec > PRICE_SECONDS_SLOTS) break;

    for (long long s = newestSec; s > oldestSec && s >= 0; s--) {
      int slot = (int)(s % PRICE_SECONDS_SLOTS);
      if (!priceSecondValid[slot] || priceSecondIds[slot] != (unsigned long)s) continue;
      writeBucketSample(baseId - b, priceSecondValue[slot], 0.0f, false);
      any = true;
      break;
    }
  }

  if (any) {
    chartLogicalBucketId = baseId;
    lastBucketAdvanceMs = nowMs;
  }
  rebuildVisibleHistory();
  rebuildFlowHistoryFromSeconds();
  chartDirty = true;
  flowDirty = true;
}

bool marketInit() {
  flowSecondBuckets = (float*)heap_caps_calloc(FLOW_SECONDS_SLOTS, sizeof(float), MALLOC_CAP_SPIRAM);
  flowSecondValid = (bool*)heap_caps_calloc(FLOW_SECONDS_SLOTS, sizeof(bool), MALLOC_CAP_SPIRAM);
  flowSecondIds = (unsigned long*)heap_caps_calloc(FLOW_SECONDS_SLOTS, sizeof(unsigned long), MALLOC_CAP_SPIRAM);
  priceSecondValue = (double*)heap_caps_calloc(PRICE_SECONDS_SLOTS, sizeof(double), MALLOC_CAP_SPIRAM);
  priceSecondValid = (bool*)heap_caps_calloc(PRICE_SECONDS_SLOTS, sizeof(bool), MALLOC_CAP_SPIRAM);
  priceSecondIds = (unsigned long*)heap_caps_calloc(PRICE_SECONDS_SLOTS, sizeof(unsigned long), MALLOC_CAP_SPIRAM);
  if (!flowSecondBuckets || !flowSecondValid || !flowSecondIds ||
      !priceSecondValue || !priceSecondValid || !priceSecondIds) {
    return false;
  }

  for (int i = 0; i < FLOW_LIVE_SLOTS; i++) {
    flowLiveSamples[i] = 0.0f;
    flowLiveValid[i] = false;
    flowLiveIds[i] = 0;
  }
  marketApplyWindow(DEFAULT_WINDOW_INDEX);
  return true;
}

void marketResetFlowClock() {
  lastFlowCalc = millis();
}

// -------------------- Precio ponderado --------------------
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

  // (7in) agresor comprador/vendedor con el mismo suavizado
  aggressorBuyBTCs  = (aggressorBuyBTCs * (1.0f - alpha)) + (binBuyAccum * toPerSecond * alpha);
  aggressorSellBTCs = (aggressorSellBTCs * (1.0f - alpha)) + (binSellAccum * toPerSecond * alpha);
  binBuyAccum = 0;
  binSellAccum = 0;

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
  // (7in) El original hacia "chartDirty = false" aca, lo que podia cancelar un
  // redibujado completo pendiente (avance de bucket) si coincidia en el mismo loop.
  chartTailDirty = true;
  flowDirty = true;
  recomputeWeightedPrice();
}

// -------------------- LED virtual (orig updateLed) --------------------
void updateVirtualLed() {
  if (weightedPrice <= 0 || histCount < 2) {
    ledR = ledG = ledB = 0;
    return;
  }

  double minP, maxP;
  computeChartRange(&minP, &maxP);

  double range = maxP - minP;
  float pos = 0.5f;
  if (range > 0.0001) {
    pos = (float)((weightedPrice - minP) / range);
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

  if (p >= 0.5f) {
    ledR = whitePart;
    ledG = LED_MAX_BRIGHTNESS;
    ledB = whitePart;
  } else {
    ledR = LED_MAX_BRIGHTNESS;
    ledG = whitePart;
    ledB = whitePart;
  }
}

uint16_t btcLabelColorFromRange() {
  if (weightedPrice <= 0 || histCount < 2) return COLOR_WHITE;

  double minP, maxP;
  computeChartRange(&minP, &maxP);

  double range = maxP - minP;
  float pos = 0.5f;
  if (range > 0.0001) {
    pos = (float)((weightedPrice - minP) / range);
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

float trendBullRatioAt(int idx) {
  if (idx <= 0 || histCount < 2 || !historyValid[idx]) return 0.5f;

  double fast = 0.0;
  double slow = 0.0;
  if (calcSMAAt(idx, SMA_FAST_PERIOD, &fast) && calcSMAAt(idx, SMA_SLOW_PERIOD, &slow) && slow > 0.0) {
    float diffPct = (float)(((fast - slow) / slow) * 100.0);
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

  double ref = history[refIdx];
  if (ref <= 0.0) return 0.5f;

  float pct = (float)(((history[idx] - ref) / ref) * 100.0);
  float ratio = 0.5f + (pct / 0.50f);
  if (ratio < 0.0f) ratio = 0.0f;
  if (ratio > 1.0f) ratio = 1.0f;
  return ratio;
}

// -------------------- Entradas WebSocket --------------------
// Misma secuencia que los callbacks originales: actualizar precio, acumular BTC,
// recalcular ponderado y, si hubo volumen, alimentar el grafico.
void onBinanceTrade(bool hasPrice, double price, bool hasQty, float qty, bool hasSide, bool buyerIsMaker) {
  if (hasPrice) {
    binance.livePrice = price;
    binance.priceOk = (binance.livePrice > 0);
    binance.lastPriceMs = millis();
  }
  tradeCountBin++;

  if (hasQty && qty > 0) {
    binance.btcAccum += qty;
    binance.lastTradeBTC = qty;
    if (hasSide) {
      // "m" = el comprador es maker -> el agresor fue el vendedor
      if (buyerIsMaker) binSellAccum += qty;
      else binBuyAccum += qty;
    }
    recomputeWeightedPrice();
    ingestChartSample(weightedPrice, totalFlowBTCs);
  } else {
    recomputeWeightedPrice();
  }
}

void onCoinbaseTicker(bool hasPrice, double price, bool hasSize, float size) {
  if (hasPrice) {
    coinbase.livePrice = price;
    coinbase.priceOk = (coinbase.livePrice > 0);
    coinbase.lastPriceMs = millis();
  }
  tradeCountCb++;

  if (hasSize && size > 0) {
    coinbase.btcAccum += size;
    coinbase.lastTradeBTC = size;
    recomputeWeightedPrice();
    ingestChartSample(weightedPrice, totalFlowBTCs);
  } else {
    recomputeWeightedPrice();
  }
}
