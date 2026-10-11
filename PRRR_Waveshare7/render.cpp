// Dibujo PRRR portado de Arduino_GFX a Canvas565.
// Las funciones conservan nombre, logica y colores del original. Cambios marcados "(7in)".
#include "render.h"
#include "market.h"
#include "color565.h"
#include <esp_timer.h>

static Canvas565* gfx = nullptr;
int g_diagLevel = PRRR_DIAG_LEVEL_DEFAULT;
RenderDiag renderDiag = {};

void renderBegin(Canvas565* canvas) {
  gfx = canvas;
}

// -------------------- Geometria --------------------
static int plotWidthPx(int w) {
  return w;
}

static int chartXForIndex(int idx, int x, int w) {
  int plotW = plotWidthPx(w);
  int px = x + (idx * CHART_COLUMN_W);
  int maxX = x + plotW - CHART_COLUMN_W;
  if (px > maxX) px = maxX;
  return px;
}

int chartYForPrice(double price, double minP, double maxP, int y, int h) {
  double range = maxP - minP;
  if (range < 0.0001) range = 1.0;
  return y + h - 2 - (int)((price - minP) / range * (h - 4));
}

static int clampChartInnerY(int py, int y, int h) {
  int minY = y + 1;
  int maxY = y + h - 3;
  if (py < minY) py = minY;
  if (py > maxY) py = maxY;
  return py;
}

// -------------------- Trazos --------------------
static void drawDualStrokeLine(int x1, int y1, int x2, int y2, uint16_t mainColor, uint16_t darkColor) {
  gfx->drawLine(x1, y1, x2, y2, mainColor);
  gfx->drawLine(x1 + 1, y1, x2 + 1, y2, darkColor);
}

static void drawDualStrokePoint(int x, int y, int w, uint16_t mainColor, uint16_t darkColor) {
  int mainW = w / 2;
  if (mainW < 1) mainW = 1;
  int darkW = w - mainW;
  if (darkW < 1) darkW = 1;
  gfx->drawFastHLine(x, y, mainW, mainColor);
  gfx->drawFastHLine(x + mainW, y, darkW, darkColor);
}

static void drawPriceStepLink(int prevX, int prevY, int currX, int currY, uint16_t color) {
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

static void drawPricePoint2px(int x, int y, uint16_t color) {
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

static uint16_t priceLineColor(double prevP, double currP) {
  if (prevP <= 0.0) return COLOR_GRAY;
  if (currP > prevP) return COLOR_PRICE_UP;
  if (currP < prevP) return COLOR_PRICE_DOWN;
  return COLOR_GRAY;
}

// -------------------- Rio de flujo --------------------
#define FLOW_GREEN_OUTER  0x00C0
#define FLOW_GREEN_MID    0x0180
#define FLOW_GREEN_CORE   0x0320
#define FLOW_RED_OUTER    0x1000
#define FLOW_RED_MID      0x2020
#define FLOW_RED_CORE     0x4020
#define FLOW_GRAY_OUTER   0x1082
#define FLOW_GRAY_MID     0x2104
#define FLOW_GRAY_CORE    0x3186

static uint16_t flowGreenColor(uint8_t tone) {
  return (tone == 0) ? FLOW_GREEN_OUTER : (tone == 1 ? FLOW_GREEN_MID : FLOW_GREEN_CORE);
}

static uint16_t flowRedColor(uint8_t tone) {
  return (tone == 0) ? FLOW_RED_OUTER : (tone == 1 ? FLOW_RED_MID : FLOW_RED_CORE);
}

static bool flowDitherPickGreen(int px, int py, float bullRatio) {
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

static void drawFlowBand(int x, int y, int w, int h) {
  if (histCount < 2) return;
  int centerY = y + h / 2;
  int maxHalfH = (h - 6) / 2;

  struct Layer { float scale; uint8_t tone; };
  Layer layers[3] = { {1.00f, 0}, {0.70f, 1}, {0.40f, 2} };

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

// -------------------- Waterfall --------------------
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
static const float WATERFALL_TREND_EPS_PCT = 0.003f;

static uint16_t waterfallRoadColorForTrend(int centerIdx, float ageFrac) {
  if (histCount < 3) return scale565(WATERFALL_ROAD_GRAY, 1.0f - ageFrac * 0.5f);

  int halfTrend = WATERFALL_TREND_WINDOW / 2;
  double leftSum = 0.0;
  double rightSum = 0.0;
  int leftCount = 0;
  int rightCount = 0;

  // (7in) El original comparaba "ri < histCount" e incluia columnas vacias (precio 0)
  // cuando el historial aun no llenaba la ventana; ahora se usan solo columnas validas.
  for (int k = 1; k <= halfTrend; k++) {
    int li = centerIdx - k;
    int ri = centerIdx + k;
    if (li >= 0 && historyValid[li]) { leftSum += history[li]; leftCount++; }
    if (ri < CHART_COLUMNS && historyValid[ri]) { rightSum += history[ri]; rightCount++; }
  }

  if (leftCount == 0 || rightCount == 0) return scale565(WATERFALL_ROAD_GRAY, 1.0f - ageFrac * 0.5f);

  double leftAvg = leftSum / leftCount;
  double rightAvg = rightSum / rightCount;
  float pct = (leftAvg > 0.0) ? (float)(((rightAvg - leftAvg) / leftAvg) * 100.0) : 0.0f;

  uint16_t baseColor = WATERFALL_ROAD_GRAY;
  if (pct > WATERFALL_TREND_EPS_PCT) baseColor = WATERFALL_ROAD_GREEN;
  else if (pct < -WATERFALL_TREND_EPS_PCT) baseColor = WATERFALL_ROAD_RED;

  return scale565(baseColor, 1.0f - ageFrac * 0.5f);
}

static void drawLiveFlowFront(int x, int y, int w, int h) {
  int plotW = plotWidthPx(w);
  int centerX = x + plotW / 2;
  int maxHalfW = (plotW - 2) / 2;
  int frontRows = h;
  if (frontRows < FLOW_LIVE_SLOTS) frontRows = FLOW_LIVE_SLOTS;
  unsigned long newestLiveId = (lastFlowCalc / flowCalcIntervalMs);
  float bullRatio = trendBullRatioAt(newestValidIndex >= 0 ? newestValidIndex : CHART_COLUMNS - 1);
  if (weightedPrice > 0.0 && prevWeightedPrice > 0.0) {
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
    if (rowY < y) break;  // (7in) frontRows puede superar h: no salirse del panel
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

static void drawFlowWaterfall(int x, int y, int w, int h) {
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

// -------------------- Grafico de precio --------------------
static void drawPriceLineOnly(int x, int y, int w, int h) {
  if (histCount < 2) return;

  double minP, maxP;
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

static void drawSMAForPeriod(int x, int y, int w, int h, int period, uint16_t color) {
  if (histCount < 2) return;

  double minP, maxP;
  computeChartRange(&minP, &maxP);
  uint16_t darkColor = (period == SMA_FAST_PERIOD) ? COLOR_SMA_FAST_DARK : COLOR_SMA_SLOW_DARK;

  int prevX = -1, prevY = -1;
  for (int i = 0; i < CHART_COLUMNS; i++) {
    double avg = 0.0;
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

static void drawTailSmaSegment(int prevIdx, int lastIdx, int prevX, int currX, double minP, double maxP,
                               int y, int h, int period, uint16_t color, uint16_t darkColor) {
  double prevSma = 0.0;
  double currSma = 0.0;
  if (!calcSMAAt(prevIdx, period, &prevSma) || !calcSMAAt(lastIdx, period, &currSma)) return;
  int smaPrevY = clampChartInnerY(chartYForPrice(prevSma, minP, maxP, y, h), y, h);
  int smaCurrY = clampChartInnerY(chartYForPrice(currSma, minP, maxP, y, h), y, h);
  drawDualStrokeLine(prevX, smaPrevY, currX, smaCurrY, color, darkColor);
  drawDualStrokePoint(currX, smaCurrY, CHART_COLUMN_W, color, darkColor);
}

// (7in) Actualizacion parcial corregida. En el original xStart = prevX + 8 quedaba a la
// derecha de xEnd con columnas de 4 px, asi que la cola nunca se redibujaba y la ultima
// columna solo se actualizaba en un redibujado completo. Ahora se limpia y redibuja
// exactamente la ultima columna.
static void drawTailRegion(int x, int y, int w, int h) {
  int lastIdx = CHART_COLUMNS - 1;
  int prevIdx = CHART_COLUMNS - 2;
  int prevX = chartXForIndex(prevIdx >= 0 ? prevIdx : 0, x, w);
  int currX = chartXForIndex(lastIdx, x, w);
  int xStart = currX;
  int xEnd = currX + CHART_COLUMN_W - 1;

  gfx->fillRect(xStart, y, xEnd - xStart + 1, h, COLOR_BLACK);

  double minP, maxP;
  computeChartRange(&minP, &maxP);

  if (historyValid[lastIdx] && prevIdx >= 0 && historyValid[prevIdx]) {
    int prevY = chartYForPrice(history[prevIdx], minP, maxP, y, h);
    int currY = chartYForPrice(history[lastIdx], minP, maxP, y, h);
    uint16_t tailColor = priceLineColor(history[prevIdx], history[lastIdx]);
    drawPriceStepLink(prevX, prevY, currX, currY, tailColor);
    drawPricePoint2px(currX, currY, tailColor);

    if (g_diagLevel >= 3) {
      drawTailSmaSegment(prevIdx, lastIdx, prevX, currX, minP, maxP, y, h, SMA_SLOW_PERIOD, COLOR_SMA_SLOW, COLOR_SMA_SLOW_DARK);
      drawTailSmaSegment(prevIdx, lastIdx, prevX, currX, minP, maxP, y, h, SMA_FAST_PERIOD, COLOR_SMA_FAST, COLOR_SMA_FAST_DARK);
    }
  }

  gfx->markDirty(prevX, y, xEnd - prevX + 1 + 1, h);
  chartTailDirty = false;
}

static void drawChart(int x, int y, int w, int h) {
  if (histCount < 2) return;  // "Esperando datos..." lo muestra una etiqueta LVGL
  drawPriceLineOnly(x, y, w, h);
  if (g_diagLevel >= 3) {
    drawSMAForPeriod(x, y, w, h, SMA_SLOW_PERIOD, COLOR_SMA_SLOW);
    drawSMAForPeriod(x, y, w, h, SMA_FAST_PERIOD, COLOR_SMA_FAST);
  }
}

static void drawPriceScaleTicks(int x, int y, int h, double minP, double maxP) {
  if (maxP <= minP) return;

  long firstTick = (long)(ceil(minP / 10.0) * 10.0);
  long lastTick = (long)(floor(maxP / 10.0) * 10.0);

  for (long tick = firstTick; tick <= lastTick; tick += 10) {
    int tickY = chartYForPrice((double)tick, minP, maxP, y, h);
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

static void drawLivePriceTail(int x, int y, int w, int h) {
  if (newestValidIndex < 0 || weightedPrice <= 0 || !hasFreshWeightedPrice()) return;

  double minP, maxP;
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
  gfx->markDirty(markerX, y, PRICE_MARKER_W, h);

  if (yLive == yHist) return;

  int top = (yLive < yHist) ? yLive : yHist;
  int height = abs(yLive - yHist) + 1;

  uint16_t c = priceLineColor(history[lastIdx], weightedPrice);
  gfx->drawFastVLine(xLast, top, height, c);
  gfx->markDirty(xLast, top, 1, height);
}

// -------------------- API --------------------
void drawStaticFrames() {
  gfx->fillRect(0, 0, gfx->width(), gfx->height(), COLOR_BLACK);
  if (g_diagLevel >= 2) gfx->drawRect(CHART_BOX_X, CHART_BOX_Y, CHART_BOX_W, CHART_BOX_H, COLOR_DARKGRAY);
  if (g_diagLevel >= 4) gfx->drawRect(FLOW_BOX_X, FLOW_BOX_Y, FLOW_BOX_W, FLOW_BOX_H, COLOR_DARKGRAY);
  if (g_diagLevel >= 5) {
    gfx->drawRect(WF_BOX_X, WF_HIST_BOX_Y, WF_BOX_W, WF_HIST_BOX_H, COLOR_DARKGRAY);
    gfx->drawRect(WF_BOX_X, WF_LIVE_BOX_Y, WF_BOX_W, WF_LIVE_BOX_H, COLOR_DARKGRAY);
  }
  gfx->markDirty(0, 0, gfx->width(), gfx->height());
}

void drawChartArea() {
  if (g_diagLevel < 2) {
    chartDirty = false;
    chartTailDirty = false;
    return;
  }
  const int64_t t0 = esp_timer_get_time();
  gfx->fillRect(CHART_INNER_X, CHART_INNER_Y, CHART_INNER_W, CHART_INNER_H, COLOR_BLACK);
  drawChart(CHART_INNER_X, CHART_INNER_Y, CHART_INNER_W, CHART_INNER_H);
  gfx->fillRect(PRICE_MARKER_X, CHART_INNER_Y, PRICE_MARKER_W, CHART_INNER_H, COLOR_BLACK);
  const uint32_t dt = (uint32_t)(esp_timer_get_time() - t0);
  renderDiag.fullCount++;
  renderDiag.fullSumUs += dt;
  if (dt > renderDiag.fullMaxUs) renderDiag.fullMaxUs = dt;
  gfx->markDirty(CHART_INNER_X, CHART_INNER_Y, PRICE_MARKER_X + PRICE_MARKER_W - CHART_INNER_X, CHART_INNER_H);
  chartDirty = false;
  chartTailDirty = false;
}

void drawTailRegionDefault() {
  if (g_diagLevel < 2) {
    chartTailDirty = false;
    return;
  }
  const int64_t t0 = esp_timer_get_time();
  drawTailRegion(CHART_INNER_X, CHART_INNER_Y, CHART_INNER_W, CHART_INNER_H);
  const uint32_t dt = (uint32_t)(esp_timer_get_time() - t0);
  renderDiag.tailCount++;
  if (dt > renderDiag.tailMaxUs) renderDiag.tailMaxUs = dt;
}

void drawLivePriceTailDefault() {
  if (g_diagLevel < 2) return;
  drawLivePriceTail(CHART_INNER_X, CHART_INNER_Y, CHART_INNER_W, CHART_INNER_H);
}

void drawFlowBandArea() {
  if (g_diagLevel < 4) return;
  gfx->fillRect(FLOW_INNER_X, FLOW_INNER_Y, FLOW_INNER_W, FLOW_INNER_H, COLOR_BLACK);
  drawFlowBand(FLOW_INNER_X, FLOW_INNER_Y, FLOW_INNER_W, FLOW_INNER_H);
  gfx->markDirty(FLOW_INNER_X, FLOW_INNER_Y, FLOW_INNER_W, FLOW_INNER_H);
}

void drawWaterfallArea() {
  if (g_diagLevel < 5) return;
  const int ix = WF_BOX_X + 1;
  const int iw = WF_BOX_W - 2;
  const int histY = WF_HIST_BOX_Y + 1;
  const int histH = WF_HIST_BOX_H - 2;
  const int liveY = WF_LIVE_BOX_Y + 1;
  const int liveH = WF_LIVE_BOX_H - 2;

  gfx->fillRect(ix, histY, iw, histH, COLOR_BLACK);
  drawFlowWaterfall(ix, histY, iw, histH);
  gfx->markDirty(ix, histY, iw, histH);

  gfx->fillRect(ix, liveY, iw, liveH, COLOR_BLACK);
  drawLiveFlowFront(ix, liveY, iw, liveH);
  gfx->markDirty(ix, liveY, iw, liveH);
}
