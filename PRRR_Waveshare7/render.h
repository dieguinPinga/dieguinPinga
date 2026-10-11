// Dibujo por pixeles del grafico, SMA, rio de flujo y waterfall.
// Funciones del sketch original, ahora sobre Canvas565 (coordenadas del lienzo).
#pragma once
#include "canvas565.h"

void renderBegin(Canvas565* canvas);
void drawStaticFrames();

int chartYForPrice(double price, double minP, double maxP, int y, int h);

void drawChartArea();            // redibujado completo del grafico (precio + SMA)
void drawTailRegionDefault();    // solo la ultima columna (actualizacion parcial)
void drawLivePriceTailDefault(); // marcador de precio en vivo + escala de ticks
void drawFlowBandArea();         // rio de flujo
void drawWaterfallArea();        // waterfall historico + frente en vivo
