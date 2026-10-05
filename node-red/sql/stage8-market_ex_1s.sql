-- PRRR etapa 8 — market_ex_1s: histórico de precio por exchange / mercado / segundo (por ahora sólo ZEC).
-- Ejecutar UNA vez como administrador (root). No toca market_1s, gmx_price ni conceptito_trades.
-- El usuario prrr ya tiene SELECT, INSERT, UPDATE sobre prrr_market.* (etapa 3): alcanza.
-- Una fila por segundo + exchange + market, sólo si ese venue tuvo trades en ese segundo (sin forward-fill).
-- Todos los DATETIME en UTC. ts = inicio del segundo según el reloj LOCAL de recepción (misma convención que market_1s.ts).
CREATE TABLE IF NOT EXISTS prrr_market.market_ex_1s (
    ts            DATETIME(3)   NOT NULL COMMENT 'inicio del segundo (recepción local, UTC)',
    symbol        VARCHAR(16)   NOT NULL,
    exchange      VARCHAR(32)   NOT NULL COMMENT 'binance, coinbase, kraken, okx, bybit, bitfinex, …',
    market        VARCHAR(16)   NOT NULL COMMENT 'spot | perp',
    last_price    DECIMAL(20,8) NOT NULL COMMENT 'precio del último trade recibido del venue en ese segundo',
    last_trade_ts DATETIME(3)   NOT NULL COMMENT 'timestamp original del exchange de ese último trade (UTC)',
    recv_ts       DATETIME(3)   NOT NULL COMMENT 'recepción local de ese último trade (UTC)',
    trades        INT           NOT NULL COMMENT 'fills del venue en el segundo (misma cuenta que market_1s.trades)',
    buy_usd       DOUBLE        NOT NULL COMMENT 'Σ precio × cantidad de trades con agresor BUY',
    sell_usd      DOUBLE        NOT NULL COMMENT 'Σ precio × cantidad de trades con agresor SELL',
    PRIMARY KEY (symbol, exchange, market, ts),
    KEY k_sym_ts (symbol, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='PRRR etapa 8: precio ZEC por exchange/mercado/segundo (base para estudiar breadth)';
