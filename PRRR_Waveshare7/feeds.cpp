#include "feeds.h"
#include "market.h"
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>

#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#else
#error "Falta wifi_secrets.h: copia wifi_secrets.example.h como wifi_secrets.h y completa tus redes."
#endif

static const int wifiCount = sizeof(WIFI_LIST) / sizeof(WIFI_LIST[0]);
static int currentWifiIdx = -1;

static WebSocketsClient wsBinance;
static WebSocketsClient wsCoinbase;

// Filtros ArduinoJson: solo se deserializan los campos usados.
static JsonDocument binanceFilter;
static JsonDocument coinbaseFilter;

const char* feedsWifiSsid() {
  if (currentWifiIdx < 0 || WiFi.status() != WL_CONNECTED) return "";
  return WIFI_LIST[currentWifiIdx].ssid;
}

// -------------------- WiFi (orig) --------------------
static bool tryConnectWifi(int idx, unsigned long timeoutMs) {
  if (idx < 0 || idx >= wifiCount) return false;

  statusText = "Conectando WiFi";
  detailText = WIFI_LIST[idx].ssid;
  appPublishStatus();

  Serial.printf("[WiFi] Probando %s...\n", WIFI_LIST[idx].ssid);

  WiFi.disconnect(true, true);
  delay(300);
  WiFi.begin(WIFI_LIST[idx].ssid, WIFI_LIST[idx].pass);

  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) delay(300);

  if (WiFi.status() == WL_CONNECTED) {
    currentWifiIdx = idx;
    statusText = "WiFi OK";
    detailText = String(WIFI_LIST[idx].ssid) + " " + WiFi.localIP().toString();
    Serial.printf("[WiFi] Conectado a %s, IP: %s\n", WIFI_LIST[idx].ssid, WiFi.localIP().toString().c_str());
    appPublishStatus();
    return true;
  }

  Serial.printf("[WiFi] Fallo con %s\n", WIFI_LIST[idx].ssid);
  return false;
}

static void connectWiFi() {
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
  appPublishStatus();
}

// -------------------- WS events --------------------
static void onBinanceEvent(WStype_t type, uint8_t* payload, size_t length) {
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
      JsonDocument doc;
      if (deserializeJson(doc, (const char*)payload, length, DeserializationOption::Filter(binanceFilter))) break;
      const char* p = doc["p"];
      const char* q = doc["q"];
      JsonVariant m = doc["m"];
      bool hasPrice = (p && strlen(p) > 0);
      bool hasQty = (q && strlen(q) > 0);
      onBinanceTrade(hasPrice, hasPrice ? strtod(p, nullptr) : 0.0,
                     hasQty, hasQty ? (float)strtod(q, nullptr) : 0.0f,
                     m.is<bool>(), m.is<bool>() ? m.as<bool>() : false);
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

static void onCoinbaseEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      coinbase.wsConnected = false;
      Serial.println("[CB] Desconectado");
      break;

    case WStype_CONNECTED: {
      coinbase.wsConnected = true;
      Serial.printf("[CB] Conectado a: %s\n", (char*)payload);

      JsonDocument doc;
      doc["type"] = "subscribe";
      JsonArray channels = doc["channels"].to<JsonArray>();
      JsonObject ch = channels.add<JsonObject>();
      ch["name"] = "ticker";
      JsonArray products = ch["product_ids"].to<JsonArray>();
      products.add("BTC-USD");

      String out;
      serializeJson(doc, out);
      wsCoinbase.sendTXT(out);
      break;
    }

    case WStype_TEXT: {
      JsonDocument doc;
      if (deserializeJson(doc, (const char*)payload, length, DeserializationOption::Filter(coinbaseFilter))) break;
      const char* typeStr = doc["type"];
      if (!typeStr) break;
      if (strcmp(typeStr, "ticker") == 0) {
        const char* p = doc["price"];
        const char* s = doc["last_size"];
        bool hasPrice = (p && strlen(p) > 0);
        bool hasSize = (s && strlen(s) > 0);
        onCoinbaseTicker(hasPrice, hasPrice ? strtod(p, nullptr) : 0.0,
                         hasSize, hasSize ? (float)strtod(s, nullptr) : 0.0f);
      } else if (strcmp(typeStr, "error") == 0) {
        const char* msg = doc["message"];
        Serial.printf("[CB] Error del servidor: %s\n", msg ? msg : "?");
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

static void connectBinanceWS() {
  wsBinance.beginSSL("data-stream.binance.vision", 443, "/ws/btcusdt@trade");
  wsBinance.onEvent(onBinanceEvent);
  wsBinance.setReconnectInterval(5000);
  wsBinance.enableHeartbeat(15000, 3000, 2);
}

static void connectCoinbaseWS() {
  wsCoinbase.beginSSL("ws-feed.exchange.coinbase.com", 443, "/");
  wsCoinbase.onEvent(onCoinbaseEvent);
  wsCoinbase.setReconnectInterval(5000);
  wsCoinbase.enableHeartbeat(15000, 3000, 2);
}

// -------------------- API --------------------
void feedsBegin() {
  binanceFilter["p"] = true;
  binanceFilter["q"] = true;
  binanceFilter["m"] = true;
  coinbaseFilter["type"] = true;
  coinbaseFilter["price"] = true;
  coinbaseFilter["last_size"] = true;
  coinbaseFilter["message"] = true;

  connectWiFi();
  if (WiFi.status() == WL_CONNECTED) {
    connectBinanceWS();
    connectCoinbaseWS();
    marketResetFlowClock();
  }
}

void feedsLoop() {
  if (WiFi.status() != WL_CONNECTED) {
    binance.wsConnected = false;
    coinbase.wsConnected = false;
    connectWiFi();
    if (WiFi.status() == WL_CONNECTED) {
      connectBinanceWS();
      connectCoinbaseWS();
      marketResetFlowClock();
    }
  }

  wsBinance.loop();
  wsCoinbase.loop();
}
