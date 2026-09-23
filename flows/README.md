# CRYPTO LITE V3 — tablero Node-RED (BTC · XMR · GMX · LTC)

Tablero liviano para Raspberry Pi lentas. Muestra las 4 monedas en un panel
resumen + una tarjeta y un gráfico por moneda, y **guarda los precios en disco
para poder ver varios días de historial** (aguanta reinicios de Node-RED).

Archivo importable (última versión): [`crypto-lite-v76.json`](./crypto-lite-v76.json)
— **fix del arpegio**: ahora va del pasado al presente (última nota = precio actual). Ver v76.

v76: **orden del arpegio corregido**. Antes tocaba actual → 1h → 2h → 4h (de ahora hacia atrás), así que
subiendo sonaba agudo→grave. Ahora va del promedio **más viejo al más nuevo** (4h → 2h → 1h) y la
**última nota es el precio actual**: si viene subiendo, la escalera va **grave→agudo** (do-re-mi) y si baja,
al revés. Se ordena por ventana automáticamente, sin importar cómo esté escrita `cryptoToneWindows`.

v75: **descarga del historial de alertas IA**. En la sección 🤖 ANÁLISIS IA hay un botón **⬇ alertas**
que baja un `.txt` con **todos los juicios** que ocurrieron (fecha y hora · situación · sesgo · texto ·
evento que lo disparó). Para que sea completo: cada juicio se **guarda en disco** (`cryptohist/ia_alerts.log`,
una línea JSON por alerta, sobrevive reinicios de Node-RED) además de mantenerse en memoria; al arrancar,
un seeder **carga el histórico del disco** para que la descarga incluya lo de sesiones anteriores. Tope de
2000 registros en memoria (el disco guarda todo).

v74: **el doble tono pasa a ser un arpegio configurable**. En cada movimiento suenan la posición
**ACTUAL** y los promedios de **1h, 2h y 4h** (todas como posición en el rango). Si las notas quedan
juntas → lateral/plano; si se abren en escalera → tendencia sostenida en esas horas (ej: actual agudo +
4h grave = venías subiendo fuerte). Las ventanas son editables en `cryptoToneWindows` (`[60,120,240]`
minutos por defecto; se pueden agregar 8h, 12h…). Historian calcula `avgwins_<moneda>` (un promedio por
ventana) cada minuto desde el historial y los feeders lo pasan al beeper; el arpegio se toca espaciado
(0.13 s por nota) y se subió el mínimo entre disparos a 450 ms para que entre completo.

v73: **doble tono según el modelo pedido**. Se vuelve al concepto original: **tono 1 = posición del precio
ACTUAL** en el rango y **tono 2 = posición del PROMEDIO** en el rango (se descarta el "intervalo por
desvío" de v72). La clave: el promedio ahora es de **1 hora** y se calcula **en el server desde el
historial por minuto** (no en un buffer del navegador), así funciona apenas se abre el tablero y sobrevive
recargas. Ventana editable en `cryptoToneAvgMin` (minutos, por defecto 60). Historian guarda
`avgwin_<moneda>` cada minuto y los feeders lo pasan al beeper en cada tick.

v72: **el doble tono ya no suena "siempre igual"**. Dos causas y dos arreglos (baratos, sin librerías):
- La ventana del promedio (tono 1) era de **2 min** → quedaba pegada al precio actual. Ahora es de
  **10 min** (`WIN_MS`, editable arriba del script del beeper). Es un simple array + promedio: costo de
  cómputo ínfimo aunque se agrande.
- El segundo tono se calculaba "redondeando" a notas sobre el rango de 3 días (enorme), así que promedio
  y actual caían casi siempre en la **misma nota**. Ahora el **tono 2 se separa del tono 1 según el
  desvío real** (% actual vs promedio): `SENS_PCT` (por defecto 0.20 % por semitono, editable). Cerca =
  misma nota (sin volatilidad); cualquier separación se **oye siempre** como intervalo proporcional. El
  tono 1 sigue marcando "dónde estás en el rango".

v71: **doble tono + rango de pool ZEC**.
- **Sonido de dos tonos**: en cada movimiento ahora suenan **dos notas**: primero la del **precio
  promedio de los últimos 2 min** y después la del **precio actual**. Si están cerca → poca volatilidad
  (casi la misma nota); si están lejos → se escucha el intervalo. Aplica a todas las monedas (BTC incluido).
  Se espacian los golpes (300 ms) para que el par no se pise.
- **ZEC · rango del pool (Orca)**: panel nuevo que muestra dónde está ZEC respecto de tu **techo
  ($1592.45)** y tu **piso ($1303.8)** del canal de liquidez: barra con la banda "en rango" en verde y un
  marcador con el precio, estado **EN RANGO ✓ (cobrando fees)** vs **FUERA ↑/↓ (sin fees)**, y cuánto
  **falta para el techo** y cuánto estás **sobre el piso**. Techo y piso editables en `cryptoZecTecho` /
  `cryptoZecPiso`. Orden del grupo ZCASH: tarjeta · rango · gauge vol · precio+EMA · histórico.

v70: **fix del sonido "escalera de notas"**. El motor usaba `playRun()` que tocaba **una nota por cada
grado intermedio** entre la nota previa y la nueva (glissando). BTC casi no lo notaba porque su rango de
3 días es enorme y el grado casi nunca cambia → una sola nota; pero XMR/GMX/ZEC, con rango más chico
relativo a sus saltos, cambiaban de varios grados por tick → **escalera**. Ahora en cada movimiento se
toca **una sola nota al tono actual** (posición en el rango), sin el glissando, así todas las monedas
suenan parejo como BTC. El volumen sigue reflejando el tamaño del movimiento.

v69: **gauge de volumen ZEC**. DexScreener ya trae el volumen por ventana (`m5`, `h1`, `h24`); ahora se
capturan las tres y se agrega un **gauge** (mismo estilo que el de FLUJO BTC) en el grupo ZCASH: la aguja
marca el **volumen de los últimos 5 min** en USD, relativo a su **máximo reciente** (con marca del
promedio de 30 min), y abajo muestra **1h** y **24h**. El parser de ZEC emite ahora dos salidas (tick +
gauge). Orden del grupo ZCASH: tarjeta · gauge · precio+EMA · histórico.

v68: **retoques de layout**.
- **ZEC · 2 min**: faltaba el chart en vivo de ZEC en el grupo EN VIVO. Se agrega (alimentado por el poll
  de ZEC, ~1 punto cada 5 s) → EN VIVO queda **2×2** (BTC, XMR, GMX, ZEC) sin el hueco que dejaba GMX solo.
- **Orden de los grupos** más homogéneo y con las monedas contiguas: RESUMEN (1) · EN VIVO (2) ·
  BITCOIN (3) · MONERO (4) · GMX (5) · ZCASH (6) · ANÁLISIS IA (7). *(Node-RED apila los grupos en
  cascada según el ancho de la ventana; con este orden y los anchos parejos queda lo más prolijo posible;
  el grupo de BTC es más alto porque tiene el gauge de flujo y el chart de doble eje.)*

v67: **el juicio de la IA pasa a ser por anomalía, no cada 5 min fijos + ZEC ahora sí trae datos**.
- **"Normal vs no normal"**: el vigía compara cada precio con **su media móvil (EMA)** y una **banda de
  tolerancia ±X%** (`cryptoIaTolPct`, por defecto 2%). Si el precio **sale de la banda**, dispara un
  juicio de la IA diciendo qué moneda y cuánto se desvió; mientras está dentro, se queda callado. Solo
  avisa en la **transición** (cuando cruza la banda), no repite mientras sigue afuera. Se mantiene el
  pico de flujo BTC como otra señal "fuera de lo normal".
- **El juicio de rutina cada 5 min pasó a cada 30 min** (`ia_inj`), como latido de fondo; el disparador
  real es la anomalía. (Si querés cero rutina, se desactiva ese inject.)
- **ZEC desde DexScreener** (el endpoint que sí funciona, pool `GTHKH…`, en `cryptoZecUrl`): trae
  **precio, volumen 24h y compras/ventas** → la tarjeta de ZEC ahora también muestra **presión
  compra/venta**. Poll cada 5 s. El parser entiende la forma de DexScreener y, de fallback, la de Orca.

v66: **se cerró la posición de BTC → se quita el break-even, y se suma ZEC (Zcash) desde Orca**.
- **Break-even de BTC eliminado**: se saca del config (`cryptoBreakeven`), de la tarjeta de BTC (fila
  Break-even), del vigía de la IA (ya no dispara por "cruce de break-even") y del beeper (sin alarma ni
  botón "🚨 probar alarma BE").
- **ZEC (Zcash) nuevo**: grupo **ZCASH · ZEC** con tarjeta, chart precio+EMA, histórico 30 días, tile en
  RESUMEN y sonido propio (instrumento **✨ cristal**). También entra al análisis de la IA.
- **Fuente = API de Orca** (pool ZEC/USDC en Solana): un poll cada 15 s trae **precio y volumen 24h** y
  alimenta toda la maquinaria genérica (anillo, EMA, min/máx robusto, Δ1h/Δ24h, histórico). El endpoint
  es configurable en `cryptoOrcaUrl` y el parser es **tolerante** (entiende la forma de Orca y también la
  de DexScreener), con anti-outlier. ZEC no tiene chart "1s en vivo" ni presión taker porque Orca no da
  datos por-trade (eso necesita el WS de un exchange); todo lo demás sí.
- **Nota**: si la notebook no llega a `api.orca.so`, la tarjeta de ZEC queda en gris (feed caído) — se
  cambia la URL en `cryptoOrcaUrl` (p. ej. a un endpoint de DexScreener del mismo pool) sin tocar nada más.

v65: **rango del día robusto + anti-glitch en las alts**. El veredicto de v64 estaba bien, pero le
entraba basura: el "rango del día" es el mín/máx del anillo de 24 h y **XMR/GMX/LTC no tenían filtro
anti-outlier** (solo BTC), así que un único tick fantasma de Kraken (ej. XMR imprimiendo $480) dejaba el
rango clavado en ~12% por hasta 24 h → VOLÁTIL falso. Dos capas de arreglo:
- **Filtro anti-outlier también en XMR/GMX/LTC** (`Guardar Kraken`): rechaza ticks que saltan más de 15%
  (`cryptoAltMaxJump`) contra el último precio bueno → no se vuelve a contaminar.
- **Mín/máx por percentiles (p2–p98)** en `Emitir tarjetas`: ignora glitches aislados **al instante**, así
  el rango vuelve a la realidad en el próximo refresco sin esperar a que el tick viejo caduque. (Verificado
  en simulación: un anillo ~528-538 con un tick a 480 daba 12.08% por min/máx y 3.33% por percentiles.)

v64: **fix de raíz de la incoherencia** (el modelo decía VOLÁTIL y después citaba −0.81% / rango 1.23%,
que son NORMAL). Un modelo de 1.7B no aplica bien los umbrales, así que ahora:
- **La SITUACIÓN y el SESGO se calculan en código** (`ia_prompt`) con los mismos umbrales
  (CALMA / NORMAL / VOLÁTIL según la mayor |d1h|, el mayor rango del día % y el flujo ×promedio). La
  insignia y la flecha salen de ahí → **nunca se contradicen** con los números.
- **La IA queda solo para redactar** la frase corta (máx 16 palabras) citando la métrica dominante; ya
  no elige el veredicto. Si Ollama no responde (`senderr:true`), se usa una **frase de respaldo armada
  en código**, así el panel y la voz **siguen funcionando aunque la IA esté caída**.
- El panel muestra la frase; la voz dice "Situación X, sesgo Y. <frase>".

v63: **la IA se pasó de larga en v62** (escribía un párrafo y encima repetía la línea "Datos:" con
todos los precios). Ahora: se le pide **UNA sola frase corta (máx 20 palabras)** citando el número
clave, con orden explícita de **no copiar la lista de datos**, y `num_predict` bajado a 110 para que no
divague. Por las dudas, el panel **recorta** cualquier "Datos:…" o reinicio de formato que se cuele y
**limita el texto a 200 caracteres**. Mantiene la exigencia de citar el número (nada de "alto/amplio"
sin cifra).

v62: **veredictos con cifras + más explicación**. Antes decía cosas vagas ("rango diario amplio y
flujo de BTC alto") sin decir cuánto. Ahora:
- El flujo se **calcula ya como ratio** (`Nx el promedio`) y el rango del día como **porcentaje**
  (`(máx−mín)/precio`), y ambos se le pasan al modelo ya masticados.
- **Umbrales numéricos también para el rango del día** (VOLÁTIL solo si algún rango > 5% o d1h > 1.5%
  o flujo > 2.5× el promedio) → deja de marcar VOLÁTIL con un rango normal de 2-3%.
- La línea **Notable** ahora pide **1-2 frases** y es **obligatorio citar el número** (el % o el
  "Nx el promedio") que dispara la situación; prohibido "alto/amplio/significativo" sin la cifra al
  lado. `num_predict` a 220 para que entren las 2 frases; el panel captura la explicación completa aunque
  venga en varias líneas.

v61: **calibración del juez IA** (el problema era que con el mercado calmo —Δ1h de 0.1/0.2/0.5%—
igual decía **VOLÁTIL** siempre, y se le escapaba el inglés). Ahora el prompt le da **umbrales
concretos**: CALMA si todas las Δ1h < 0.5%, NORMAL entre 0.5% y 1.5%, y **VOLÁTIL solo** si alguna
Δ1h supera 1.5% (o hay pico de flujo / rango diario amplio), con la regla "ante la duda, el nivel más
bajo". Responde en un **formato fijo de 3 líneas en español** (Situación / Sesgo / Notable), que el
panel **parsea** para pintar la insignia y la flecha; el texto grande muestra solo lo **Notable** (sin
repetir la situación que ya está en la insignia) y la **voz** dice una frase compuesta
("Situación normal, sesgo lateral. …"). `temperature` a 0.45 para que no repita tanto.

v60: **IA con voz + reactiva + panel vivo**. Tres cosas nuevas sobre el juez de v59:
- **Mensajes de voz (TTS)**: el panel lee el veredicto **en voz alta** apenas se actualiza, usando la
  voz en español del navegador (`speechSynthesis`). Botón **🔊/🔇** para prender/apagar (se recuerda en
  `localStorage`) y **▶** para repetir. Si la situación **escala a VOLÁTIL**, la voz avisa con
  **"¡Atención!"** al frente y un tono más marcado. (La voz suena en la máquina donde se ve el
  dashboard; requiere una interacción previa en la página, como cualquier audio del navegador.)
- **Análisis reactivo por evento**: además del análisis **cada 5 min**, un vigía cada 45 s dispara un
  análisis **extra** cuando pasa algo notable — **cruce del break-even de BTC**, **movimiento fuerte**
  (Δ1h ≥ 1.5 %) o **pico de flujo** (≥ 2.2× el promedio reciente). Tiene *cooldown* de 90 s para no
  saturar a Ollama, y el veredicto muestra **⚡ disparado por: …**.
- **Panel coloreado + historial**: el borde/insignia toma color según la situación
  (**verde** CALMA · **azul** NORMAL · **rojo** VOLÁTIL), con **flecha de sesgo** (▲ alcista / ▼ bajista
  / ▶ lateral) y una **lista de los últimos veredictos** con su hora y color.

**IMPORTANTE:** importá **reemplazando** (no "copiar"), si no quedan nodos viejos duplicados. Todo lo
demás igual que v59 (Ollama en CPU, `num_gpu: 0`).

v59: **Ollama forzado a CPU** (`options.num_gpu: 0`): en la notebook la GPU colgaba a Ollama (de ahí el
"no response from server"); en CPU responde. Ese era el fix de fondo del análisis IA. El resto igual
que v58 (contexto global + prompt robusto).

v58: **fix del análisis IA por contexto**: `cardsSummary` (y `mktBtcEma`) ahora se guardan **también como
`global`**, y "Armar prompt IA" lee `flow.get() || global.get()`. Así funciona aunque los nodos hayan
quedado en tabs/copias distintas (que era por qué la IA no se disparaba: `flow.get` no veía el dato y
cortaba antes de llamar a Ollama). Además el armado del prompt es más robusto (usa `Number()` para no
romper con `toFixed` si algún valor viene como string).

v57: **fix real del flood "WebSocket is not open"**: en vez de mandar el subscribe a Kraken por un
timer "a ciegas" cada 60 s (que fallaba justo en los micro‑cortes/reconexiones), ahora el subscribe
se dispara **solo cuando llega un mensaje del WS** (Kraken v2 manda `status` al conectar y
`heartbeat` seguido), o sea cuando el socket está **abierto de verdad** → el envío nunca falla. El
inject de 60 s queda inerte y se re‑suscribe solo al reconectar. **IMPORTANTE:** importá
**reemplazando** (no "copiar"), si no quedan los nodos viejos conviviendo y el flood sigue.

v56: gate por `krakenUp` (no alcanzaba para los micro‑cortes) + `senderr` en los http request.

v55: **fix del análisis IA (Ollama) que daba "no response from server"**: se **acota la salida** del
modelo (`options.num_predict: 220`, `temperature: 0.3`) para que no genere de más y se pase del
tiempo, y se pone un **timeout de request explícito de 120 s** (`msg.requestTimeout`). Además los
pedidos de volumen a Binance/Coinbase ahora tienen **timeout corto (8 s)** para que fallen rápido si
la red los bloquea (antes quedaban colgados). El resto del juez IA igual que v54.

v54: **juez IA local (Ollama)**: cada **5 min** arma un resumen del mercado (precios, Δ1h/Δ24h, rango,
presión compra/venta, break-even, flujo BTC/min) y le pregunta a **qwen3:1.7b** (`127.0.0.1:11434`,
`stream:false`, `think:false`) que diga si la situación es **CALMA/NORMAL/VOLÁTIL**, el sesgo y algo
notable, en 2 frases. Se muestra en un panel nuevo **🤖 ANÁLISIS IA**. Si Ollama no está corriendo,
el panel avisa y conserva el último análisis. Requiere Ollama con el modelo `qwen3:1.7b` local.

v53: **histórico (30 días) también para XMR y GMX** (path generalizado, un chart por moneda).

v52: **chart "BTC · histórico (30 días)"** que lee todo el `.log` de disco y muestra mucha más
antigüedad que el principal (hasta 3 días); guardado en disco subido a 30 días. Se subió el guardado a
**30 días** (`cryptoKeepDays`) y se agregó `cryptoHistDays` (ventana del histórico). Trae precio +
línea de precio actual + break-even, downsample a 600 puntos, eje X con fecha. Ojo: arranca
mostrando solo lo ya grabado (~1 día) y se llena hasta 30 días con el tiempo.

v51: **chart doble eje: Flujo (BTC/min) vs PRECIO** (1 pto/s, ventana 1 min). Se cambió la 2ª línea de
USD/min a **precio** porque USD/min = BTC/min × precio (proporcionales) y se superponían; volumen y
precio **no** son proporcionales, así que se cruzan de verdad y muestran la relación volumen↔precio.
**Precio** a la izquierda (azul), **BTC/min** a la derecha (dorado); eje X mm:ss.

v50: chart doble eje BTC/min vs USD/min a 1 pto/s, ventana 1 min (las dos líneas se superponían por
ser proporcionales; v51 cambia USD por precio).

v49: **chart de flujo con DOBLE EJE Y** (SVG a medida, node-red-dashboard no soporta doble eje):
USD/min izq, BTC/min der; v50 lo pasa a 1 pto/s con ventana de 1 min.

v48: chart BTC/min vs USD/min normalizado a % (descartado; v49 lo reemplaza por doble eje real).

v47: **el gauge de flujo pasa a ser multi‑exchange y muestra BTC + USD**: Node‑RED suma el volumen del
último minuto de **Binance** (ticker rolling 1 m) + **Coinbase** (candles 60 s) + **Kraken** (del feed
de trades que ya teníamos), cada 20 s, con **fallback**: si la red del server no deja pasar
Binance/Coinbase, cae a lo que haya (mínimo Kraken). Muestra el caudal en **BTC/min** (grande) y su
equivalente en **USD/min** (≈ BTC×precio), y abajo qué **fuentes** están vivas (B/C/K). Sigue
suavizado (EMA) y con escala relativa a lo reciente.

v46: **gauge de flujo BTC/min** (del `flow_btc_m` del sensor), suavizado y con escala relativa a lo
reciente. v47 lo reemplaza por el cálculo multi‑exchange de Node‑RED.

v45: **volumen relativo al promedio de las operaciones**: una operación del **tamaño promedio reciente
= volumen estándar**; más chica → más bajo (con un **piso** para que igual se escuche); más grande →
más fuerte (con techo). Se normaliza contra lo que viene pasando en cada moneda (`ratio =
cambio_actual / promedio_reciente`, gain 0.28–0.9). El tono sigue siendo la posición en el rango.

v44: **un "plu" por CADA cambio de precio** (fuente reactiva MQTT/WS, no el muestreador de 1 s);
antes el volumen quedó fijo — v45 lo hace relativo al promedio.

v43: suena en cada movimiento (antes solo al cruzar una nota); tono = posición en el rango.

v42: **el tono mapea el RANGO del gráfico**: mínimo ploteado = nota más grave, máximo = más aguda;
el precio actual cae en un punto entre medio. El chip muestra la **posición en el rango** (0–100%).

v41: **se sacó Litecoin** (tarjeta, gráfico, gráfico en vivo, tile del resumen y su sonido) y **el
break‑even pasó a Bitcoin en $79,613.63**. Ahora BTC muestra su break‑even como **fila en la tarjeta**
(% arriba/abajo), **línea dorada en el gráfico** y **alarma sonora al cruzarlo** (con el botón
🚨 de prueba). El tablero queda con **3 monedas**: BTC, XMR, GMX. (El backend sigue leyendo LTC de
Kraken pero no se muestra en ningún lado; si querés lo saco del todo.)

v40: **botón "🚨 probar alarma BE"** para disparar la sirena + cartel al toque.

v39: **línea de precio actual en los 4 gráficos grandes**: una línea horizontal (blanca) al precio
de ahora, con el precio en la etiqueta de la leyenda (ej. `BTC ● $79,870`). Para que quede plana
de ancho completo se cambió el dibujado: el seeder **re-dibuja cada 30 s** (re-lee el histórico en
disco, así se mantiene la vista de varios días) y el Historian dejó de hacer append a los gráficos
(sigue guardando a disco y calculando EMA). Nota: node-red-dashboard no permite líneas punteadas
en el chart, así que va sólida y en color distinto (blanca) para que se distinga.

v38: **escalón del sonido por PORCENTAJE** (0.02% del promedio ~1h = una nota, `STEP_PCT`, igual
para las 4 monedas); el chip muestra el % sobre el promedio.

v37: **arregla el "tambor"**: do central = promedio corto (~1h, cerca del precio) y el sonido
dispara solo al cruzar un escalón (antes era por $10 fijo; ahora por %).

v36: **sonido = altura de precio (modelo).** Do central = promedio; cada paso de precio = un grado
de la escala mayor; el tono dice el nivel y el gesto la dirección. (El "tambor" se corrigió en v37.)

v35: **flash mucho más suave**: se sacó el "flip" pleno del número del precio; ahora queda blanco en
los movimientos chicos y solo se tiñe suave en los fuertes; fondo bien tenue. Referencia percentil 80.

v34: **flash con más rango dinámico**: la referencia pasó al **percentil 80** de los cambios
recientes (no el promedio) para que los movimientos normales no saturen. (Igual el número seguía
haciendo flip pleno — corregido en v35.)

v33: **flash más expresivo + sonido melódico**. El parpadeo verde/rojo se normaliza contra el
cambio reciente de cada moneda y el sonido
**camina una escala pentatónica** por moneda con su instrumento: sube el precio → sube un grado
(do→re→mi…), baja → baja un grado; rachas de suba suenan como melodía ascendente. El botón ▶
corre la escala del instrumento.

v32: **ajustes de layout**: **RESUMEN** y **EN VIVO** pasan a **16** de ancho; los 4 gráficos de
1s pasan a **8** (2×2 llenando el ancho); la **tarjeta BTC** sube a alto **7** y la **LTC** a **6**.

v31: **márgenes del eje Y aún más ceñidos**: usa el **mínimo y máximo reales** de la ventana con
apenas **6 %** de aire y pisos más chicos, así la curva llena el gráfico. (No se pueden clavar
`ymin/ymax` exactos porque en node-red-dashboard eso solo va con `ui_control`, que borra los
puntos cada vez — por eso se usan las series min/max invisibles, bien apretadas.)

v30: **eje Y de los gráficos EN VIVO ajustado**: el margen dejó de ser fijo ($15 en BTC aplastaba
la curva cuando el precio se movía centavos) y pasó a ser **proporcional al rango real** de la
ventana con un piso chico por moneda. El rango incluye Binance/Coinbase para que las 3 líneas entren.

v29: **break-even de LTC en 55.57** (línea del gráfico + fila de la tarjeta) y **alarma sonora al
cruzar la barrera**: cuando el precio de LTC cruza el break-even (para arriba o para abajo) suena
una **sirena molesta** (onda cuadrada, fuerte) e imposible de confundir con los ticks normales,
más un cartel ⚠ que dice hacia dónde cruzó. Tiene banda muerta (histéresis) y cooldown para no
repetir en el borde. El valor se edita en el beeper (`var BE = { LTC: 55.57 }`) y en el config
(`cryptoBreakeven`). La sirena ignora el mute por moneda (siempre te avisa) pero respeta el ON/OFF
y el volumen general.

v28: **el sonido ahora suena parejo en las 4 monedas**: antes el beeper se alimentaba de BTC (MQTT,
~2/s) y de los ticks esporádicos de Kraken, así que casi solo se escuchaba BTC. Ahora lo alimenta
el **muestreador de 1s** (tiene el precio de las 4, refrescado por REST cada 2s), con cadencia
pareja y sin depender del WebSocket. Cada moneda suena cuando **realmente** se mueve.

v27: **sonido activado por default**: viene en ON y se **auto-desbloquea con el primer clic/tecla**
en cualquier parte de la página (los navegadores no dejan sonar sin una interacción; ya no hace
falta buscar el botón). El botón pasó a ser ON/OFF (🔊/🔇) y la elección se recuerda.

v26: **un instrumento distinto por moneda** (sintetizado con armónicos + envolvente ADSR, no las
ondas crudas): **BTC 🔔 campana**, **XMR 🪵 marimba**, **GMX 🎸 cuerda pulsada**, **LTC 🎹 órgano**,
separados por octavas. La dirección es un **gesto de 2 notas** (sube = ascendente / baja =
descendente) en vez de glissando, mucho más claro. Volumen proporcional a la magnitud. Botón
**▶** por moneda para escuchar cada instrumento.

v25: una voz por moneda con ondas crudas (sine/triangle/square/sawtooth) + glissando — quedaban
parecidas entre sí; v26 las reemplaza por instrumentos sintetizados bien distintos.

v24: **ticks sonoros integrados**: el beeper (Web Audio) ya viene cableado al feed **real**
(BTC por MQTT + XMR/GMX/LTC por Kraken), así **cada variación de precio suena**. Vive arriba de
los gráficos EN VIVO: apretá **🔓 Activar sonido** una vez (el navegador lo exige), regulá el
volumen y hacé **clic en cada moneda para silenciarla** (se recuerda). El beep es proporcional
(volumen = magnitud vs volatilidad 2 min; tono agudo+verde = sube, grave+rojo = baja).

v23: **aprovecha toda la telemetría MQTT de BTC**: además del precio compuesto ahora usa
`binance` y `coinbase` **por separado**, sus edades (`binance_age_ms`/`coinbase_age_ms`),
`flow_btc_m` y la **señal `LONG/SHORT`** (`signal`/`signal_source`/`signal_samples`) que ya
mandaba el sensor y se estaban descartando. En el **gráfico BTC en vivo** se ven 3 líneas
(compuesto en dorado, Binance en cian, Coinbase en azul, con leyenda); en la **tarjeta BTC**
hay filas de Binance/Coinbase con su edad, el **spread CB−BIN** ($ y %) y un **badge verde/rojo**
con la señal.

v22: **filtro anti-outlier en BTC**: rechaza saltos absurdos vs el último precio bueno
(`cryptoBtcMaxJump`, 15%) → un glitch del sensor (ej. 100k) ya no ensucia el histórico.
Para purgar datos malos existentes, usar [`reset-btc-snippet.json`](./reset-btc-snippet.json).

**Probar los parlantes:** [`audio-test-snippet.json`](./audio-test-snippet.json) — flujo
autocontenido (crea su propia pestaña **🔊 TEST AUDIO**) con dos botones: uno hace sonar el
**SERVER** (la notebook que corre Node-RED, vía un nodo `exec` que prueba `paplay`→`aplay`→
`speaker-test`) y otro suena en el **NAVEGADOR** donde ves el dashboard (nodo `ui_audio`, voz
del navegador). El botón del server muestra el resultado (`✅ rc 0` o el error) en la tarjeta
"Estado server". Si el server no suena aunque los parlantes andan, casi siempre es que Node-RED
corre como servicio sin sesión de audio: instalá `pulseaudio-utils`/`alsa-utils`, o arrancá
Node-RED desde tu sesión de escritorio.

**Alertas sonoras de precio (tick-flash sonoro):**
[`audio-tick-snippet.json`](./audio-tick-snippet.json) — flujo autocontenido (pestaña
**🔊 TICKS SONOROS**) que convierte cada cambio de precio en un **beep proporcional**, la versión
audible del tick-flash: **volumen = magnitud** del cambio (normalizada por la volatilidad de los
últimos 2 min, como la opacidad del parpadeo) y **tono/color = dirección** (sube = beep agudo
+ verde, baja = beep grave + rojo). Usa **Web Audio** dentro de un `ui_template` (no `ui_audio`,
que solo hace TTS). Trae un **simulador** (inject "▶ demo") para escucharlo al instante.
- Primero apretá **🔓 Activar audio** (el navegador exige un click para habilitar sonido).
- Para engancharlo al **precio real**: en el editor, cableá las salidas de tick en vivo del flujo
  principal hacia el nodo `ui_template` **beeper** — la de *BTC guardar* (`22a9a563f73205fe`) y las
  4 de *Guardar Kraken* (`942b2551f3443d26`), que ya emiten `{payload: precio, topic: 'BTC'|…}`.
  Ahí desactivá/borrá el inject "▶ demo".

v21: **BTC = solo MQTT como verdad del precio**, gana el mensaje con **timestamp más nuevo**;
Kraken/REST ya no tocan el precio de BTC (solo vol24/bid/ask).

v20: BTC por MQTT primario (Kraken/REST eran respaldo del precio si MQTT callaba >15s).

v19: **WS reactivo** (tick directo a los gráficos en vivo), historial a 30s y trades a 10s.

v18: "power pack" — historial 1 punto/min, **doble EMA** (lenta 20 + rápida 9,
`cryptoEmaFast`), trades a 20s.

v17: **BTC con 2 decimales** (tarjeta y resumen; antes iba redondeado).

v16: **ticker REST a 2s** (más fluido) y **margen fijo por moneda** en los gráficos en vivo
(evita el overshoot del eje): BTC +$15, XMR +$0.50, GMX +$0.02, LTC +$0.40 (editable en
*Precio actual (1s)*).

v15 **corta la línea en los huecos**
de los 4 gráficos grandes (apagón/feed caído) con un punto `null`, para no "pegar" datos
de timestamps separados. Umbral configurable `cryptoGapMin` (10 min). También reinicia la
EMA tras el hueco.

v14 unificó el fondo (`#0d1117`) y agregó identidad de color por moneda (borde + símbolo).

v12 agregó **margen de eje** en los gráficos EN VIVO (2 series invisibles piso/techo,
sin `ui_control`, no borra datos). Los gráficos grandes quedan sin margen todavía.

v11 sincronizó el flash con los gráficos en vivo (tarjetas a 1s).

v10 hizo el **tick-flash proporcional**:
la opacidad del parpadeo depende del tamaño del cambio, normalizado por la volatilidad
de los últimos 2 min (cambio chico → flash tenue; salto grande → flash fuerte). En
tarjetas y resumen, vía `ng-style` (no toca los datos del gráfico).
v9 subió el poll de trades a 30s.

⚠️ `crypto-lite-v8.json` quedó DESCARTADO: intentaba dar padding al eje Y con
`ui_control`, pero en node-red-dashboard eso **borra los puntos del gráfico** cada vez
que se reenvía (no borra el disco, solo el dibujo). v9 revierte ese cambio.
v7 llevó el tick-flash al panel RESUMEN.
v6 agregó el tick-flash en las tarjetas de detalle; sobre
[`crypto-lite-v5.json`](./crypto-lite-v5.json).
v5 trae **break-even** por moneda (fila % en tarjeta + línea amarilla en el gráfico,
config `cryptoBreakeven`, ej. LTC 54.85), sobre la base hand-tuned
[`crypto-lite-v4.json`](./crypto-lite-v4.json). El [`crypto-lite-v3.json`](./crypto-lite-v3.json) queda de referencia.

---

## Qué trae

- **Panel RESUMEN** arriba: las 4 monedas en una grilla 2×2 con precio,
  variación % de la última hora (verde/rojo) y "frescura" del dato en segundos.
- **EN VIVO · 1s**: 4 gráficos chiquitos (grilla 2×2) con 1 punto por segundo y
  ventana de 2 min. El eje Y se auto-ajusta → "zoom" de los micro-movimientos del
  precio actual. Son efímeros: **no** se guardan a disco.
- **Por moneda**: una tarjeta de detalle enriquecida + un gráfico de precio con
  **media móvil (EMA)** superpuesta.
  - Tarjeta: **Mín/Máx** (con barra de rango que marca dónde está el precio hoy),
    **Δ1h y Δ24h**, y para las de Kraken **Vol 24h + nº de operaciones**.
  - BTC: precio/vol/presión por WebSocket de Kraken (par XBT/USD), **más** el
    flujo BTC/s, promedio y RSSI de tu sensor por MQTT (se muestran como "Sensor").
  - XMR / GMX / LTC: además bid, ask, edad del feed, y **presión compra/venta**
    (últimos 5 min, barra verde/roja) vía el canal de *trades* de Kraken.
- **Historial en disco**: 1 punto cada 3 min por moneda, guardado en
  `~/.node-red/cryptohist/<MONEDA>.log` (un JSON por línea). Al arrancar,
  Node-RED recarga ese historial en los gráficos (por defecto muestra 3 días).
- **Recorte automático** cada 6 h para que los archivos no crezcan sin fin
  (por defecto conserva 7 días).

## Por qué es liviano

- Las **tarjetas** se refrescan rápido (cada 5 s) → se siente ágil.
- Los **gráficos e historial** van despacio (1 punto cada 3 min) → con 3 días
  son ~1.440 puntos por gráfico: poca memoria, poca escritura a disco y el
  navegador no se ahoga.
- Al cargar el historial se hace **downsample** a máx. 600 puntos por gráfico.
- Los gráficos **EN VIVO (1s)** son livianos: ventana de 2 min = ~120 puntos c/u,
  y no tocan disco. Si tu feed no actualiza tan seguido, la línea queda escalonada
  (muestra el último precio conocido); si el feed se cae >30 s, deja de dibujar.
- Usa solo **nodos nativos** de Node-RED (`file` / `file in`): no hay que
  instalar SQLite ni módulos extra.

## Requisitos

- Node-RED con **node-red-dashboard** instalado.
- Para BTC: un broker MQTT (por defecto `127.0.0.1:1883`, ej. Mosquitto local)
  publicando en `btc/#` un JSON tipo:
  `{"price":65000,"flow_btc_s":0.12,"rssi":-70,"device":"esp32"}`.
- Para BTC/XMR/GMX/LTC: salida a internet hacia `wss://ws.kraken.com/v2`
  (tiempo real, WebSocket v2 de Kraken) y a `https://api.kraken.com` (latido
  REST de respaldo). Kraken es de los pocos exchanges que aún lista XMR (Monero).

## Cómo importar

1. En Node-RED: menú (☰) → **Import** → pegá el contenido de
   `crypto-lite-v3.json` → **Import**.
2. **Deploy**.
3. Abrí el dashboard en `http://<ip-de-la-pi>:1880/ui` → pestaña
   **CRYPTO LITE V3**.

La carpeta `cryptohist/` se crea sola bajo el directorio de Node-RED
(`~/.node-red/`). El historial se ve completo recién después de tener la Pi
prendida un rato (se llena a 1 punto/min).

## Ajustes (nodo "Config al arrancar")

Editá el nodo function **Set config global** para cambiar el comportamiento:

| Variable            | Default        | Qué hace                                        |
|---------------------|----------------|-------------------------------------------------|
| `cryptoHistBase`    | `cryptohist/`  | Carpeta donde se guardan los `.log`.            |
| `cryptoViewDays`    | `3`            | Días que se **muestran** en los gráficos al cargar. |
| `cryptoKeepDays`    | `7`            | Días que se **guardan** en disco.               |
| `cryptoStaleSec`    | `600`          | Segs sin dato para marcar la moneda como "caída" (gris). |
| `cryptoEmaN`        | `20`           | Períodos de la media móvil (EMA). A 3 min/punto ≈ 1 h.  |

- ¿Querés ver más días en el gráfico? Subí `cryptoViewDays` y también
  el `removeOlder` de cada nodo `ui_chart` (viene en 3 días). Ojo: más puntos =
  más trabajo para el navegador. Si vas a muchos días, conviene además espaciar
  el guardado (nodo **Historial · cada 3min**: subí `repeat` a 300 = 5 min).
- ¿Querés guardar más historia? Subí `cryptoKeepDays`. El disco usado es chico
  (~30 bytes por punto → ~4 KB por moneda por día).

## Notas

- Si un feed no llega en `cryptoStaleSec` (10 min por defecto), la moneda
  aparece atenuada con un punto rojo. Kraken solo manda precio cuando hay
  operaciones, así que monedas de bajo volumen (GMX, XMR) pueden estar minutos
  "quietas" sin estar caídas: por eso el umbral es amplio.
- **Ticker REST** (nodo *REST ticker · 20s*): cada 20 s trae precio/vol de las
  4 monedas por HTTP. Es la fuente principal si el WebSocket no llega.
- **Trades REST** (nodo *REST trades · 60s*): cada 60 s trae las operaciones
  recientes (con lado compra/venta) por HTTP y alimenta las **barras de presión**
  sin depender del WebSocket. Ideal si tu red bloquea WebSockets.
- Kraken pide `GMX/USD`; si tu Kraken no lista ese par, esa tarjeta quedará
  vacía (las otras funcionan igual).
- **Presión compra/venta**: se calcula de las operaciones reales de Kraken
  (canal *trades*, últimos 5 min). En monedas de bajo volumen (GMX) puede tardar
  en tener datos hasta que ocurra alguna operación. **BTC no tiene split** real:
  el feed MQTT manda un flujo único. Por eso BTC ahora también toma precio y
  operaciones del WebSocket de Kraken (XBT/USD) y así tiene barra de presión real.
- **BTC: dos fuentes.** Kraken (XBT/USD) manda el precio de mercado, volumen y
  presión; tu sensor MQTT aporta flujo/RSSI/device (línea "Sensor") y queda de
  respaldo del precio si el WebSocket de Kraken se cae >2 min.
- **Vol 24h / operaciones**: vienen en el ticker de Kraken (ticker + REST), sin
  costo extra, ahora también para BTC.
- La **EMA** se reconstruye desde el historial en disco al arrancar, así que la
  línea de tendencia aparece completa apenas cargás.
- Para que el historial sobreviva reinicios **de la Pi**, alcanza con este
  esquema (se guarda en archivos); no depende de la persistencia de contexto de
  Node-RED.
