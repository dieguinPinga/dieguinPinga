#include "ui.h"
#include "prrr_config.h"
#include <atomic>

// -------------------- Fuentes (con respaldo si no estan en lv_conf.h) --------------------
#if LV_FONT_MONTSERRAT_12
#define FONT_12 &lv_font_montserrat_12
#else
#define FONT_12 LV_FONT_DEFAULT
#endif
#if LV_FONT_MONTSERRAT_14
#define FONT_14 &lv_font_montserrat_14
#else
#define FONT_14 LV_FONT_DEFAULT
#endif
#if LV_FONT_MONTSERRAT_16
#define FONT_16 &lv_font_montserrat_16
#else
#define FONT_16 LV_FONT_DEFAULT
#endif
#if LV_FONT_MONTSERRAT_20
#define FONT_20 &lv_font_montserrat_20
#else
#define FONT_20 FONT_16
#endif
#if LV_FONT_MONTSERRAT_24
#define FONT_24 &lv_font_montserrat_24
#else
#define FONT_24 FONT_20
#endif
#if LV_FONT_MONTSERRAT_28
#define FONT_28 &lv_font_montserrat_28
#else
#define FONT_28 FONT_24
#endif
#if LV_FONT_MONTSERRAT_48
#define FONT_HUGE &lv_font_montserrat_48
#elif LV_FONT_MONTSERRAT_40
#define FONT_HUGE &lv_font_montserrat_40
#elif LV_FONT_MONTSERRAT_32
#define FONT_HUGE &lv_font_montserrat_32
#else
#define FONT_HUGE FONT_28
#endif

static std::atomic<int> requestedWindow(-1);

static lv_obj_t* canvasObj;
static lv_obj_t* ledDot;
static lv_obj_t* lblBtc;
static lv_obj_t* lblPrice;
static lv_obj_t* lblPct;
static lv_obj_t* lblHighLow;
static lv_obj_t* lblFlow;
static lv_obj_t* trendBox;
static lv_obj_t* lblTrend;
static lv_obj_t* binDot;
static lv_obj_t* lblBin;
static lv_obj_t* cbDot;
static lv_obj_t* lblCb;
static lv_obj_t* weightBar;
static lv_obj_t* weightFill;
static lv_obj_t* aggrBar;
static lv_obj_t* aggrFill;
static lv_obj_t* lblAggr;
static lv_obj_t* lblWaiting;
static lv_obj_t* lblWindowInfo;
static lv_obj_t* lblStatus;
static lv_obj_t* scaleLabels[UI_SCALE_LABELS];
static lv_obj_t* windowBtns[WINDOW_PRESET_COUNT];

static const int WEIGHT_BAR_W = 232;

// Cache para no invalidar objetos que no cambiaron
static int lastTrend = -1;
static int lastActiveWindow = -1;

static lv_color_t c565(uint16_t c) {
  uint8_t r = (c >> 11) & 0x1F;
  uint8_t g = (c >> 5) & 0x3F;
  uint8_t b = c & 0x1F;
  return lv_color_make((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2));
}

static lv_obj_t* makeLabel(lv_obj_t* parent, const lv_font_t* font, uint16_t color, int x, int y, const char* text) {
  lv_obj_t* l = lv_label_create(parent);
  lv_obj_set_style_text_font(l, font, 0);
  lv_obj_set_style_text_color(l, c565(color), 0);
  lv_label_set_text(l, text);
  lv_obj_set_pos(l, x, y);
  return l;
}

static lv_obj_t* makeBox(lv_obj_t* parent, int x, int y, int w, int h, uint16_t bg, int radius) {
  lv_obj_t* o = lv_obj_create(parent);
  lv_obj_remove_style_all(o);
  lv_obj_set_pos(o, x, y);
  lv_obj_set_size(o, w, h);
  lv_obj_set_style_bg_color(o, c565(bg), 0);
  lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
  lv_obj_set_style_radius(o, radius, 0);
  lv_obj_clear_flag(o, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
  return o;
}

static void setTextIfChanged(lv_obj_t* l, const char* text) {
  const char* cur = lv_label_get_text(l);
  if (cur && strcmp(cur, text) == 0) return;
  lv_label_set_text(l, text);
}

static void setTextColor(lv_obj_t* l, uint16_t color) {
  lv_color_t c = c565(color);
  if (lv_obj_get_style_text_color(l, 0).full == c.full) return;
  lv_obj_set_style_text_color(l, c, 0);
}

static void setBgColor(lv_obj_t* o, lv_color_t c) {
  if (lv_obj_get_style_bg_color(o, 0).full == c.full) return;
  lv_obj_set_style_bg_color(o, c, 0);
}

static void setWidthIfChanged(lv_obj_t* o, int w) {
  if (lv_obj_get_style_width(o, 0) == w) return;
  lv_obj_set_width(o, w);
}

static void setHidden(lv_obj_t* o, bool hidden) {
  if (lv_obj_has_flag(o, LV_OBJ_FLAG_HIDDEN) == hidden) return;
  if (hidden) lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
  else lv_obj_clear_flag(o, LV_OBJ_FLAG_HIDDEN);
}

static void onWindowButton(lv_event_t* e) {
  int idx = (int)(intptr_t)lv_event_get_user_data(e);
  requestedWindow.store(idx);
}

int uiTakeRequestedWindow() {
  return requestedWindow.exchange(-1);
}

lv_obj_t* uiCanvas() {
  return canvasObj;
}

void uiInit(lv_color_t* canvasBuffer, int canvasW, int canvasH) {
  lv_obj_t* scr = lv_scr_act();
  lv_obj_set_style_bg_color(scr, c565(COLOR_BLACK), 0);
  lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
  lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

  // Lienzo primero: las etiquetas quedan por encima
  canvasObj = lv_canvas_create(scr);
  lv_canvas_set_buffer(canvasObj, canvasBuffer, canvasW, canvasH, LV_IMG_CF_TRUE_COLOR);
  lv_obj_set_pos(canvasObj, CANVAS_X, CANVAS_Y);
  lv_obj_clear_flag(canvasObj, LV_OBJ_FLAG_CLICKABLE);

  // ---- Cabecera: BTC/USD y precio grande ----
  ledDot = makeBox(scr, 12, 10, 18, 18, COLOR_BLACK, LV_RADIUS_CIRCLE);
  lv_obj_set_style_border_color(ledDot, c565(COLOR_DARKGRAY), 0);
  lv_obj_set_style_border_width(ledDot, 1, 0);
  lblBtc = makeLabel(scr, FONT_20, COLOR_WHITE, 38, 8, "BTC/USD");
  lblPrice = makeLabel(scr, FONT_HUGE, COLOR_WHITE, 12, 34, "---");

  lblPct = makeLabel(scr, FONT_24, COLOR_GRAY, 400, 10, "");
  lblHighLow = makeLabel(scr, FONT_20, COLOR_LIGHTGRAY, 400, 50, "");
  lv_label_set_recolor(lblHighLow, true);

  // ---- Cabecera derecha: BTC/s y LONG/SHORT ----
  lblFlow = makeLabel(scr, FONT_28, COLOR_CYAN, 0, 6, "BTC/s 0,00");
  lv_obj_align(lblFlow, LV_ALIGN_TOP_RIGHT, -12, 6);

  trendBox = makeBox(scr, 604, 46, 188, 38, COLOR_BLACK, 6);
  lv_obj_set_style_border_width(trendBox, 2, 0);
  lv_obj_set_style_border_color(trendBox, c565(COLOR_DARKGRAY), 0);
  lblTrend = lv_label_create(trendBox);
  lv_obj_set_style_text_font(lblTrend, FONT_24, 0);
  lv_obj_set_style_text_color(lblTrend, c565(COLOR_LIGHTGRAY), 0);
  lv_label_set_text(lblTrend, "-");
  lv_obj_center(lblTrend);

  // ---- Fila de exchanges ----
  binDot = makeBox(scr, 12, 97, 14, 14, COLOR_DARKGRAY, LV_RADIUS_CIRCLE);
  lblBin = makeLabel(scr, FONT_16, COLOR_BINANCE, 32, 94, "BINANCE --");
  cbDot = makeBox(scr, 250, 97, 14, 14, COLOR_DARKGRAY, LV_RADIUS_CIRCLE);
  lblCb = makeLabel(scr, FONT_16, COLOR_COINBASE, 270, 94, "COINBASE --");
  lv_obj_set_style_text_color(lblCb, lv_color_make(0x40, 0x90, 0xFF), 0);

  makeLabel(scr, FONT_12, COLOR_GRAY, 478, 90, "PESO");
  weightBar = makeBox(scr, 560, 91, WEIGHT_BAR_W, 12, COLOR_COINBASE, 3);
  weightFill = makeBox(weightBar, 0, 0, WEIGHT_BAR_W / 2, 12, COLOR_BINANCE, 0);

  lblAggr = makeLabel(scr, FONT_12, COLOR_GRAY, 478, 106, "AGRESOR");
  aggrBar = makeBox(scr, 560, 107, WEIGHT_BAR_W, 12, COLOR_DARKGRAY, 3);
  aggrFill = makeBox(aggrBar, 0, 0, 0, 12, 0x05E0, 0);

  // ---- Leyendas sobre el lienzo ----
  lv_obj_t* legend = makeLabel(scr, FONT_14, COLOR_LIGHTGRAY, CHART_INNER_X + 6, CANVAS_Y + CHART_INNER_Y + 3, "");
  lv_label_set_recolor(legend, true);
#if PRRR_ENABLE_SMA
  lv_label_set_text(legend, "PRECIO  #4060FF SMA25#  #FF2000 SMA55#");
#else
  lv_label_set_text(legend, "PRECIO");
#endif
#if PRRR_ENABLE_FLOW_BAND
  makeLabel(scr, FONT_12, COLOR_GRAY, FLOW_INNER_X + 4, CANVAS_Y + FLOW_INNER_Y + 2, "FLUJO BTC/s (color: tendencia SMA)");
#endif
#if PRRR_ENABLE_WATERFALL
  makeLabel(scr, FONT_12, COLOR_GRAY, WF_BOX_X + 5, CANVAS_Y + WF_HIST_BOX_Y + 3, "WATERFALL");
  makeLabel(scr, FONT_12, COLOR_GRAY, WF_BOX_X + 5, CANVAS_Y + WF_LIVE_BOX_Y + 3, "VIVO");
#endif

  lblWaiting = makeLabel(scr, FONT_20, COLOR_LIGHTGRAY, 0, 0, "Esperando datos...");
  lv_obj_set_pos(lblWaiting, CHART_INNER_X + CHART_INNER_W / 2 - 90, CANVAS_Y + CHART_INNER_Y + CHART_INNER_H / 2 - 10);

  for (int i = 0; i < UI_SCALE_LABELS; i++) {
    scaleLabels[i] = makeLabel(scr, FONT_14, COLOR_LIGHTGRAY, PRICE_LABEL_X, 0, "");
    lv_obj_add_flag(scaleLabels[i], LV_OBJ_FLAG_HIDDEN);
  }

  // ---- Fila inferior: botones de ventana + estado ----
  const int btnY = CANVAS_Y + CANVAS_H + 6;
  const int btnW = 80;
  const int btnH = SCREEN_H - btnY - 4;
  for (int i = 0; i < WINDOW_PRESET_COUNT; i++) {
    lv_obj_t* b = lv_btn_create(scr);
    lv_obj_set_pos(b, 6 + i * (btnW + 6), btnY);
    lv_obj_set_size(b, btnW, btnH);
    lv_obj_set_style_radius(b, 6, 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_set_style_border_width(b, 1, 0);
    lv_obj_set_style_border_color(b, c565(COLOR_DARKGRAY), 0);
    lv_obj_set_style_bg_color(b, lv_color_make(0x20, 0x20, 0x20), 0);
    lv_obj_set_style_bg_color(b, c565(COLOR_ORANGE), LV_STATE_CHECKED);
    lv_obj_set_style_text_color(b, c565(COLOR_WHITE), 0);
    lv_obj_set_style_text_color(b, c565(COLOR_BLACK), LV_STATE_CHECKED);
    lv_obj_add_event_cb(b, onWindowButton, LV_EVENT_CLICKED, (void*)(intptr_t)i);
    lv_obj_t* l = lv_label_create(b);
    lv_obj_set_style_text_font(l, FONT_20, 0);
    lv_label_set_text(l, WINDOW_PRESETS[i].label);
    lv_obj_center(l);
    windowBtns[i] = b;
  }

  const int infoX = 6 + WINDOW_PRESET_COUNT * (btnW + 6) + 6;
  lblWindowInfo = makeLabel(scr, FONT_16, COLOR_LIGHTGRAY, infoX, btnY, "");
  lv_obj_set_width(lblWindowInfo, SCREEN_W - infoX - 6);
  lv_label_set_long_mode(lblWindowInfo, LV_LABEL_LONG_DOT);
  lblStatus = makeLabel(scr, FONT_14, COLOR_GRAY, infoX, btnY + 22, "Boot...");
  lv_obj_set_width(lblStatus, SCREEN_W - infoX - 6);
  lv_label_set_long_mode(lblStatus, LV_LABEL_LONG_DOT);
}

void uiApply(const UiSnapshot& s) {
  setTextIfChanged(lblPrice, s.price);
  setTextColor(lblPrice, s.priceColor);
  setTextColor(lblBtc, s.btcColor);

  // LED virtual: el NeoPixel del original, escalado a brillo completo
  lv_color_t led = lv_color_make((uint16_t)s.ledR * 255 / LED_MAX_BRIGHTNESS,
                                 (uint16_t)s.ledG * 255 / LED_MAX_BRIGHTNESS,
                                 (uint16_t)s.ledB * 255 / LED_MAX_BRIGHTNESS);
  setBgColor(ledDot, led);

  setTextIfChanged(lblPct, s.pct);
  setTextColor(lblPct, s.pctColor);
  setTextIfChanged(lblHighLow, s.highLow);
  setTextIfChanged(lblFlow, s.flow);

  if (s.trend != lastTrend) {
    lastTrend = s.trend;
    uint16_t col = COLOR_LIGHTGRAY;
    const char* txt = "-";
    if (s.trend == 1) { col = COLOR_GREEN; txt = "LONG"; }
    else if (s.trend == 2) { col = COLOR_SMA_SLOW; txt = "SHORT"; }
    lv_label_set_text(lblTrend, txt);
    lv_obj_set_style_text_color(lblTrend, c565(col), 0);
    lv_obj_set_style_border_color(trendBox, c565(s.trend == 0 ? COLOR_DARKGRAY : col), 0);
    lv_obj_center(lblTrend);
  }

  setTextIfChanged(lblBin, s.binLine);
  setBgColor(binDot, c565(s.binDotColor));
  setTextIfChanged(lblCb, s.cbLine);
  setBgColor(cbDot, c565(s.cbDotColor));

  if (s.weightsValid) {
    setBgColor(weightBar, c565(COLOR_COINBASE));
    setWidthIfChanged(weightFill, (int)(s.binWeight * WEIGHT_BAR_W + 0.5f));
  } else {
    setBgColor(weightBar, c565(COLOR_DARKGRAY));
    setWidthIfChanged(weightFill, 0);
  }

  if (s.aggressorValid) {
    setBgColor(aggrBar, c565(0xA800));
    setWidthIfChanged(aggrFill, (int)(s.buyRatio * WEIGHT_BAR_W + 0.5f));
  } else {
    setBgColor(aggrBar, c565(COLOR_DARKGRAY));
    setWidthIfChanged(aggrFill, 0);
  }
  setTextIfChanged(lblAggr, s.aggressor);

  setHidden(lblWaiting, !s.waiting);

  for (int i = 0; i < UI_SCALE_LABELS; i++) {
    if (i < s.scaleCount) {
      setTextIfChanged(scaleLabels[i], s.scaleText[i]);
      int y = s.scaleY[i] - 8;
      if (lv_obj_get_y(scaleLabels[i]) != y) lv_obj_set_y(scaleLabels[i], y);
      setHidden(scaleLabels[i], false);
    } else {
      setHidden(scaleLabels[i], true);
    }
  }

  if (s.activeWindow != lastActiveWindow) {
    lastActiveWindow = s.activeWindow;
    for (int i = 0; i < WINDOW_PRESET_COUNT; i++) {
      if (i == s.activeWindow) lv_obj_add_state(windowBtns[i], LV_STATE_CHECKED);
      else lv_obj_clear_state(windowBtns[i], LV_STATE_CHECKED);
    }
  }

  setTextIfChanged(lblWindowInfo, s.windowInfo);
  setTextIfChanged(lblStatus, s.status);
}
