// Modelo de mercado PRRR: precio ponderado por flujo, buckets, SMA, flujo BTC/s.
// Es la logica del sketch original, separada del dibujo. Todo se ejecuta en la
// tarea de loop() (red + calculo); LVGL nunca se toca desde aca.
#pragma once
#include <Arduino.h>
#include "prrr_config.h"

struct ExchangeData {
  const char* label;
  uint16_t color;
  double livePrice;        // (7in) double: BTC/USD con centavos sin perder precision
  float weight;
  bool wsConnected;
  bool priceOk;
  unsigned long lastPriceMs;
  float btcAccum;
  float flowBTCs;
  float lastTradeBTC;
};

enum CrossSignal {
  SIGNAL_FLAT = 0,
  SIGNAL_LONG,
  SIGNAL_SHORT
};

extern ExchangeData binance;
extern ExchangeData coinbase;

extern double weightedPrice;
extern double prevWeightedPrice;
extern double firstWeightedPrice;
extern bool hasFirstWeighted;
extern String statusText;
extern String detailText;
extern float totalFlowBTCs;
extern CrossSignal currentTrendSignal;

// Ventana activa
extern int currentWindowIndex;
extern unsigned long chartWindowMs;      // orig CHART_WINDOW_MS
extern unsigned long flowWindowSeconds;  // orig CHART_WINDOW_SECONDS
extern unsigned long bucketMs;           // orig BUCKET_MS

// Historial visible
extern double history[CHART_COLUMNS];
extern float flowHistory[CHART_COLUMNS];
extern bool historyValid[CHART_COLUMNS];
extern unsigned long historyBucketIds[CHART_COLUMNS];
extern int histCount;
extern int newestValidIndex;

// Flujo en vivo (frente del waterfall)
extern unsigned long lastFlowCalc;
extern float flowLiveSamples[FLOW_LIVE_SLOTS];
extern bool flowLiveValid[FLOW_LIVE_SLOTS];
extern unsigned long flowLiveIds[FLOW_LIVE_SLOTS];

// Flags de redibujado
extern bool chartDirty;
extern bool chartTailDirty;
extern bool flowDirty;

// LED virtual (antes NeoPixel)
extern float ledSmoothedPos;
extern uint8_t ledR, ledG, ledB;

// Agresor comprador/vendedor (solo Binance, campo "m"). Agregado nuevo, no existia.
extern float aggressorBuyBTCs;
extern float aggressorSellBTCs;

// Contadores para el resumen por Serial
extern uint32_t tradeCountBin;
extern uint32_t tradeCountCb;

bool marketInit();
void marketApplyWindow(int idx);
void marketResetFlowClock();

bool isFresh(const ExchangeData& ex);
bool hasFreshWeightedPrice();
int firstValidHistoryIndex();
void computeChartRange(double* outMin, double* outMax);
bool computeVisibleHighLow(double* outLow, double* outHigh);
bool calcSMAAt(int idx, int period, double* outAvg);
float trendBullRatioAt(int idx);

void recomputeWeightedPrice();
void updateFlows();
void updateVirtualLed();
uint16_t btcLabelColorFromRange();

// Entradas desde los WebSockets
void onBinanceTrade(bool hasPrice, double price, bool hasQty, float qty, bool hasSide, bool buyerIsMaker);
void onCoinbaseTicker(bool hasPrice, double price, bool hasSize, float size);
