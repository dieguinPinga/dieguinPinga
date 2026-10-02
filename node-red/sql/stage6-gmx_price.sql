-- PRRR etapa 6 — tabla independiente para ZEC_GMX (precio de ejecución GMX v2 desde el oracle keeper).
-- Ejecutar UNA vez como administrador (root). market_1s no se toca.
-- El usuario prrr ya tiene SELECT, INSERT, UPDATE sobre prrr_market.* (etapa 3), que cubre esta tabla.
CREATE TABLE IF NOT EXISTS prrr_market.gmx_price (
  ts         DATETIME(3)     NOT NULL COMMENT 'recepción local (UTC, ms)',
  source_ts  DATETIME(3)     NOT NULL COMMENT 'updatedAt del oracle keeper (UTC, ms)',
  symbol     VARCHAR(16)     NOT NULL COMMENT 'ZEC',
  price      DECIMAL(30,18)  NOT NULL COMMENT '(min_price + max_price) / 2 (derivado)',
  min_price  DECIMAL(30,18)  NOT NULL COMMENT 'minPrice del oráculo (bid): cierre long / apertura short',
  max_price  DECIMAL(30,18)  NOT NULL COMMENT 'maxPrice del oráculo (ask): apertura long / cierre short',
  age_ms     INT             NULL     COMMENT 'ts - source_ts (incluye desfase de relojes)',
  rtt_ms     INT             NULL     COMMENT 'ida y vuelta HTTP de la consulta',
  source     VARCHAR(96)     NOT NULL COMMENT 'host + ruta del oracle keeper',
  PRIMARY KEY (symbol, source_ts),
  KEY idx_symbol_ts (symbol, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='PRRR etapa 6: ZEC_GMX (una fila por publicación real de la fuente)';
-- Si el usuario prrr NO tuviera permisos sobre toda la base:
-- GRANT SELECT, INSERT, UPDATE ON prrr_market.gmx_price TO 'prrr'@'localhost';
