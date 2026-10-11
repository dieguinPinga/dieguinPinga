// WiFi + WebSockets Binance/Coinbase. Los callbacks solo actualizan el modelo
// (market.cpp); nunca llaman a LVGL. Todo corre en la tarea de loop().
#pragma once
#include <Arduino.h>

struct WifiNet {
  const char* ssid;
  const char* pass;
};

void feedsBegin();
void feedsLoop();
const char* feedsWifiSsid();   // red actual o "" si no hay

// La define el .ino: refresca la cabecera mientras connectWiFi() bloquea.
void appPublishStatus();
