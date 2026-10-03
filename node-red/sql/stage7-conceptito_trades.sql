-- PRRR etapa 7 — CONCEPTITO LIVE: operaciones paper (sin dinero real) del simulador ZEC SMA39/76.
-- Ejecutar UNA vez como administrador (root). No toca market_1s ni gmx_price.
-- El usuario prrr ya tiene SELECT, INSERT, UPDATE sobre prrr_market.* (etapa 3): alcanza (no hace falta DELETE).
-- Una fila por operación: se inserta con status='OPEN' al abrir (así sobrevive a un Deploy/restart) y se completa al cerrar.
-- Todos los DATETIME en UTC.
-- Si ya habías creado la versión anterior de la tabla (sin signal_ts / exit_trigger_price), como todavía no se instaló, lo más simple es:
--   DROP TABLE prrr_market.conceptito_trades;   y volver a ejecutar este archivo.
CREATE TABLE IF NOT EXISTS prrr_market.conceptito_trades (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  symbol           VARCHAR(16)    NOT NULL COMMENT 'ZEC',
  source           VARCHAR(16)    NOT NULL COMMENT 'PRRR = precio de señal y de ejecución paper (buckets 1 s)',
  status           ENUM('OPEN','CLOSED') NOT NULL,
  signal_ts        DATETIME(3)    NULL     COMMENT 'inicio del bucket 1 s donde se confirmó el cruce SMA (N) (UTC)',
  entry_ts         DATETIME(3)    NOT NULL COMMENT 'inicio del bucket de entrada = N+1 (UTC); = market_1s.ts',
  exit_ts          DATETIME(3)    NULL     COMMENT 'inicio del bucket 1 s de la salida (UTC)',
  side             ENUM('LONG','SHORT') NOT NULL,
  entry_price      DECIMAL(20,8)  NOT NULL COMMENT 'close del bucket de entrada (N+1)',
  exit_price       DECIMAL(20,8)  NULL     COMMENT 'TP/SL: precio equivalente al PnL realizado fijo · TIMEOUT: close del bucket de salida',
  exit_trigger_price DECIMAL(20,8) NULL    COMMENT 'close del bucket que disparó la salida (detección de TP/SL o close del TIMEOUT)',
  qty              DECIMAL(24,10) NOT NULL COMMENT 'exposure_usd / entry_price',
  margin_usd       DECIMAL(12,2)  NOT NULL,
  leverage         DECIMAL(6,2)   NOT NULL,
  exposure_usd     DECIMAL(12,2)  NOT NULL,
  sma_fast         SMALLINT       NOT NULL COMMENT 'período (s)',
  sma_slow         SMALLINT       NOT NULL COMMENT 'período (s)',
  sma_fast_entry   DECIMAL(20,8)  NULL     COMMENT 'valor de SMA rápida en el cruce',
  sma_slow_entry   DECIMAL(20,8)  NULL     COMMENT 'valor de SMA lenta en el cruce',
  tp_usd           DECIMAL(12,2)  NOT NULL COMMENT 'PnL bruto objetivo',
  sl_usd           DECIMAL(12,2)  NOT NULL COMMENT 'PnL bruto límite (negativo)',
  timeout_min      DECIMAL(6,2)   NOT NULL,
  fee_rate         DECIMAL(8,6)   NOT NULL COMMENT 'por lado, sobre el nocional de ese lado',
  gross_pnl        DECIMAL(14,4)  NULL     COMMENT 'TP: exactamente tp_usd · SL: exactamente sl_usd · TIMEOUT: PnL real al close',
  fee_entry        DECIMAL(12,4)  NOT NULL,
  fee_exit         DECIMAL(12,4)  NULL,
  fees             DECIMAL(12,4)  NULL     COMMENT 'fee_entry + fee_exit',
  net_pnl          DECIMAL(14,4)  NULL     COMMENT 'gross_pnl - fees',
  exit_reason      ENUM('TP','SL','TIMEOUT') NULL,
  duration_seconds INT            NULL,
  mfe_usd          DECIMAL(14,4)  NULL     COMMENT 'máximo PnL bruto durante la operación',
  mae_usd          DECIMAL(14,4)  NULL     COMMENT 'mínimo PnL bruto durante la operación',
  PRIMARY KEY (id),
  UNIQUE KEY uq_symbol_entry (symbol, entry_ts),
  KEY idx_symbol_status (symbol, status),
  KEY idx_exit (symbol, exit_ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='PRRR etapa 7: CONCEPTITO LIVE (paper trading, sin dinero real)';
