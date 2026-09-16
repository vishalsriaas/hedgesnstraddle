-- Reference target schema: PostgreSQL. Design artifact, not an applied migration.
-- All IDs are application-generated UUIDs. All financial input comes from decimal strings.
-- MONEY has 8 settlement decimals; PRICE/QTY have 12. Reject overflow/extra input precision.
-- Immutable source rows: instruments, market_events, fills, lot_matches, journal,
-- order_events, session_events. Corrections use reversal/replacement events.
BEGIN;
CREATE DOMAIN trading_decimal AS NUMERIC(38,12)
  CHECK (VALUE NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric));
CREATE DOMAIN trading_money AS NUMERIC(38,8)
  CHECK (VALUE NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric));

CREATE TABLE wallets (
  wallet_id UUID PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  currency TEXT NOT NULL CHECK(currency='USDT'),
  mode TEXT NOT NULL CHECK(mode IN ('PAPER','LIVE')),
  created_at TIMESTAMPTZ NOT NULL,
  accounting_version TEXT NOT NULL
);

CREATE TABLE instruments (
  instrument_id UUID PRIMARY KEY,
  venue TEXT NOT NULL,
  market TEXT NOT NULL CHECK(market IN ('USDT_LINEAR_FUTURE','USDT_PREMIUM_OPTION')),
  exchange_symbol TEXT NOT NULL,
  underlying TEXT NOT NULL,
  base_currency TEXT NOT NULL,
  quote_currency TEXT NOT NULL CHECK(quote_currency='USDT'),
  settlement_currency TEXT NOT NULL CHECK(settlement_currency='USDT'),
  expiry_at TIMESTAMPTZ,
  strike trading_decimal,
  option_right TEXT CHECK(option_right IN ('CALL','PUT')),
  contract_multiplier trading_decimal NOT NULL CHECK(contract_multiplier>0),
  price_tick trading_decimal NOT NULL CHECK(price_tick>0),
  quantity_step trading_decimal NOT NULL CHECK(quantity_step>0),
  min_quantity trading_decimal NOT NULL CHECK(min_quantity>0),
  metadata_hash TEXT NOT NULL,
  metadata_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE(venue, market, exchange_symbol),
  CHECK((market='USDT_PREMIUM_OPTION' AND expiry_at IS NOT NULL AND strike>0 AND option_right IS NOT NULL)
     OR (market='USDT_LINEAR_FUTURE' AND strike IS NULL AND option_right IS NULL))
);

CREATE TABLE engine_runs (
  run_id UUID PRIMARY KEY,
  worker_name TEXT NOT NULL,
  host_name TEXT NOT NULL,
  process_id BIGINT NOT NULL,
  source_hash TEXT NOT NULL,
  source_root TEXT NOT NULL,
  database_identity TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  heartbeat_at TIMESTAMPTZ NOT NULL,
  stopped_at TIMESTAMPTZ
);

CREATE TABLE strategy_sessions (
  session_id UUID PRIMARY KEY,
  wallet_id UUID NOT NULL REFERENCES wallets,
  strategy TEXT NOT NULL CHECK(strategy IN ('STRADDLE','HEDGE')),
  role TEXT NOT NULL CHECK(role IN ('STRADDLE','TRADER_1','TRADER_2')),
  expiry_session_date DATE NOT NULL,
  state TEXT NOT NULL,
  config_json JSONB NOT NULL,
  config_hash TEXT NOT NULL,
  entry_start_at TIMESTAMPTZ NOT NULL,
  entry_end_at TIMESTAMPTZ NOT NULL,
  futures_cutoff_at TIMESTAMPTZ NOT NULL,
  squareoff_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  version BIGINT NOT NULL DEFAULT 0,
  UNIQUE(wallet_id,strategy,role,expiry_session_date),
  CHECK(entry_start_at<=entry_end_at AND entry_end_at<=squareoff_at),
  CHECK(futures_cutoff_at<=squareoff_at)
);

CREATE TABLE session_events (
  event_id UUID PRIMARY KEY,
  session_id UUID NOT NULL REFERENCES strategy_sessions,
  idempotency_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  payload_json JSONB NOT NULL,
  effective_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL,
  run_id UUID REFERENCES engine_runs
);

CREATE TABLE market_events (
  market_event_id UUID PRIMARY KEY,
  instrument_id UUID NOT NULL REFERENCES instruments,
  source TEXT NOT NULL,
  source_event_key TEXT NOT NULL,
  event_kind TEXT NOT NULL CHECK(event_kind IN ('MARK','BOOK','TRADE','SETTLEMENT')),
  exchange_at TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  received_monotonic_ns BIGINT NOT NULL,
  source_sequence BIGINT,
  mark_price trading_decimal CHECK(mark_price>=0),
  bid_price trading_decimal CHECK(bid_price>=0),
  ask_price trading_decimal CHECK(ask_price>=0),
  bid_qty trading_decimal CHECK(bid_qty>=0),
  ask_qty trading_decimal CHECK(ask_qty>=0),
  trade_price trading_decimal CHECK(trade_price>=0),
  trade_qty trading_decimal CHECK(trade_qty>0),
  settlement_price trading_decimal CHECK(settlement_price>=0),
  book_levels_json JSONB,
  raw_archive_uri TEXT,
  raw_sha256 TEXT NOT NULL,
  UNIQUE(source,instrument_id,source_event_key),
  UNIQUE(market_event_id,instrument_id)
);
CREATE INDEX market_events_lookup ON market_events(instrument_id,exchange_at DESC);

CREATE TABLE orders (
  order_id UUID PRIMARY KEY,
  session_id UUID NOT NULL REFERENCES strategy_sessions,
  instrument_id UUID NOT NULL REFERENCES instruments,
  client_order_id TEXT NOT NULL UNIQUE,
  exchange_order_id TEXT,
  side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  position_side TEXT NOT NULL CHECK(position_side IN ('LONG','SHORT')),
  intent TEXT NOT NULL CHECK(intent IN ('OPEN','CLOSE','SETTLE')),
  order_type TEXT NOT NULL CHECK(order_type IN ('MARKET','LIMIT','STOP_MARKET','STOP_LIMIT','SETTLEMENT')),
  time_in_force TEXT NOT NULL CHECK(time_in_force IN ('GTC','GTD','IOC','FOK')),
  quantity trading_decimal NOT NULL CHECK(quantity>0),
  limit_price trading_decimal CHECK(limit_price>0),
  stop_price trading_decimal CHECK(stop_price>0),
  stop_reference TEXT CHECK(stop_reference IN ('MARK','LAST','INDEX')),
  reduce_only BOOLEAN NOT NULL,
  oco_group_id UUID,
  parent_order_id UUID REFERENCES orders,
  status TEXT NOT NULL CHECK(status IN ('NEW','ACKNOWLEDGED','PARTIALLY_FILLED','FILLED','CANCEL_PENDING','CANCELLED','EXPIRED','REJECTED')),
  submitted_at TIMESTAMPTZ NOT NULL,
  accepted_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL,
  version BIGINT NOT NULL DEFAULT 0,
  UNIQUE(order_id,instrument_id),
  CHECK(order_type NOT IN ('LIMIT','STOP_LIMIT') OR limit_price IS NOT NULL),
  CHECK(order_type NOT IN ('STOP_LIMIT','STOP_MARKET') OR (stop_price IS NOT NULL AND stop_reference IS NOT NULL))
);
CREATE INDEX orders_working ON orders(instrument_id,status,expires_at);

CREATE TABLE order_events (
  event_id UUID PRIMARY KEY,
  order_id UUID NOT NULL REFERENCES orders,
  source TEXT NOT NULL,
  source_event_key TEXT NOT NULL,
  old_status TEXT,
  new_status TEXT NOT NULL,
  reason TEXT,
  effective_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL,
  payload_json JSONB NOT NULL,
  UNIQUE(source,source_event_key)
);

CREATE TABLE fills (
  fill_id UUID PRIMARY KEY,
  order_id UUID NOT NULL,
  instrument_id UUID NOT NULL,
  source TEXT NOT NULL,
  source_execution_id TEXT NOT NULL,
  execution_kind TEXT NOT NULL CHECK(execution_kind IN ('TRADE','SETTLEMENT')),
  quantity trading_decimal NOT NULL CHECK(quantity>0),
  price trading_decimal NOT NULL CHECK(price>=0),
  fee_usdt trading_money NOT NULL, -- positive=expense; negative=rebate
  original_fee_currency TEXT NOT NULL,
  original_fee_amount trading_decimal NOT NULL,
  fee_conversion_rate trading_decimal NOT NULL CHECK(fee_conversion_rate>0),
  fee_conversion_source TEXT NOT NULL,
  market_event_id UUID,
  liquidity TEXT CHECK(liquidity IN ('MAKER','TAKER','NA')),
  executed_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL,
  reversal_of UUID REFERENCES fills,
  FOREIGN KEY(order_id,instrument_id) REFERENCES orders(order_id,instrument_id),
  FOREIGN KEY(market_event_id,instrument_id) REFERENCES market_events(market_event_id,instrument_id),
  UNIQUE(source,order_id,source_execution_id),
  UNIQUE(fill_id,instrument_id),
  CHECK(execution_kind='SETTLEMENT' OR price>0)
);

CREATE TABLE position_lots (
  lot_id UUID PRIMARY KEY,
  session_id UUID NOT NULL REFERENCES strategy_sessions,
  instrument_id UUID NOT NULL REFERENCES instruments,
  opening_fill_id UUID NOT NULL UNIQUE,
  position_side TEXT NOT NULL CHECK(position_side IN ('LONG','SHORT')),
  opened_qty trading_decimal NOT NULL CHECK(opened_qty>0),
  remaining_qty trading_decimal NOT NULL CHECK(remaining_qty>=0 AND remaining_qty<=opened_qty),
  entry_price trading_decimal NOT NULL CHECK(entry_price>0),
  initial_option_basis trading_money NOT NULL CHECK(initial_option_basis>=0),
  remaining_option_basis trading_money NOT NULL CHECK(remaining_option_basis>=0 AND remaining_option_basis<=initial_option_basis),
  opened_at TIMESTAMPTZ NOT NULL,
  closed_at TIMESTAMPTZ,
  version BIGINT NOT NULL DEFAULT 0,
  FOREIGN KEY(opening_fill_id,instrument_id) REFERENCES fills(fill_id,instrument_id),
  UNIQUE(lot_id,instrument_id)
);
CREATE INDEX lots_fifo ON position_lots(session_id,instrument_id,position_side,opened_at,lot_id);

CREATE TABLE lot_matches (
  match_id UUID PRIMARY KEY,
  instrument_id UUID NOT NULL REFERENCES instruments,
  opening_lot_id UUID NOT NULL,
  closing_fill_id UUID NOT NULL,
  matched_qty trading_decimal NOT NULL CHECK(matched_qty>0),
  released_option_basis trading_money NOT NULL CHECK(released_option_basis>=0),
  option_proceeds trading_money NOT NULL,
  gross_realized_pnl trading_money NOT NULL,
  matched_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY(opening_lot_id,instrument_id) REFERENCES position_lots(lot_id,instrument_id),
  FOREIGN KEY(closing_fill_id,instrument_id) REFERENCES fills(fill_id,instrument_id),
  UNIQUE(opening_lot_id,closing_fill_id)
);

-- A balanced event journal. B = cash + historical cost of still-open long options.
-- Per row: delta(B) = net realized PnL + external capital movement.
CREATE TABLE journal (
  journal_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id UUID NOT NULL UNIQUE,
  wallet_id UUID NOT NULL REFERENCES wallets,
  session_id UUID REFERENCES strategy_sessions,
  event_type TEXT NOT NULL CHECK(event_type IN ('FILL','FUNDING','DEPOSIT','WITHDRAWAL','TRANSFER','REVERSAL')),
  source TEXT NOT NULL,
  source_event_key TEXT NOT NULL,
  fill_id UUID UNIQUE REFERENCES fills,
  transfer_group_id UUID,
  cash_delta trading_money NOT NULL,
  option_basis_delta trading_money NOT NULL,
  gross_realized_pnl trading_money NOT NULL,
  fee_expense trading_money NOT NULL,
  funding_income trading_money NOT NULL,
  net_realized_pnl trading_money NOT NULL,
  net_deposit trading_money NOT NULL,
  effective_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL,
  trading_date DATE NOT NULL,
  reversal_of BIGINT UNIQUE REFERENCES journal,
  evidence_json JSONB NOT NULL,
  UNIQUE(wallet_id,source,source_event_key),
  CHECK(net_realized_pnl=gross_realized_pnl-fee_expense+funding_income),
  CHECK(cash_delta+option_basis_delta=net_realized_pnl+net_deposit),
  CHECK((event_type='FILL')=(fill_id IS NOT NULL))
);
CREATE INDEX journal_period ON journal(wallet_id,trading_date,journal_id);

-- Rebuildable projections; never update from a configuration form.
CREATE TABLE wallet_balances (
  wallet_id UUID PRIMARY KEY REFERENCES wallets,
  cash_balance trading_money NOT NULL,
  open_option_basis trading_money NOT NULL CHECK(open_option_basis>=0),
  lifetime_net_realized trading_money NOT NULL,
  lifetime_net_deposits trading_money NOT NULL,
  reserved_cash trading_money NOT NULL CHECK(reserved_cash>=0),
  last_journal_id BIGINT REFERENCES journal,
  version BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  CHECK(cash_balance+open_option_basis=lifetime_net_realized+lifetime_net_deposits)
);

CREATE TABLE valuation_snapshots (
  snapshot_id UUID PRIMARY KEY,
  wallet_id UUID NOT NULL REFERENCES wallets,
  as_of TIMESTAMPTZ NOT NULL,
  ledger_watermark BIGINT REFERENCES journal,
  wallet_version BIGINT NOT NULL,
  cash_balance trading_money NOT NULL,
  open_option_basis trading_money NOT NULL,
  book_balance trading_money NOT NULL,
  unrealized_pnl trading_money,
  equity trading_money,
  quality TEXT NOT NULL CHECK(quality IN ('VALID','MISSING_QUOTE','STALE_QUOTE','RECONCILIATION_ERROR')),
  CHECK(book_balance=cash_balance+open_option_basis),
  CHECK((quality='VALID' AND unrealized_pnl IS NOT NULL AND equity=book_balance+unrealized_pnl)
     OR (quality<>'VALID' AND unrealized_pnl IS NULL AND equity IS NULL))
);
CREATE TABLE position_marks (
  snapshot_id UUID NOT NULL REFERENCES valuation_snapshots,
  lot_id UUID NOT NULL,
  instrument_id UUID NOT NULL,
  market_event_id UUID,
  remaining_qty trading_decimal NOT NULL CHECK(remaining_qty>0),
  entry_price trading_decimal NOT NULL CHECK(entry_price>0),
  option_basis trading_money NOT NULL,
  mark_price trading_decimal CHECK(mark_price>=0),
  unrealized_pnl trading_money,
  quality TEXT NOT NULL,
  PRIMARY KEY(snapshot_id,lot_id),
  FOREIGN KEY(lot_id,instrument_id) REFERENCES position_lots(lot_id,instrument_id),
  FOREIGN KEY(market_event_id,instrument_id) REFERENCES market_events(market_event_id,instrument_id)
);

CREATE TABLE daily_states (
  wallet_id UUID NOT NULL REFERENCES wallets,
  trading_date DATE NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>0),
  period_start TIMESTAMPTZ NOT NULL,
  period_end TIMESTAMPTZ NOT NULL,
  opening_book_balance trading_money NOT NULL,
  opening_cash trading_money NOT NULL,
  opening_option_basis trading_money NOT NULL,
  ending_book_balance trading_money NOT NULL,
  ending_cash trading_money NOT NULL,
  ending_option_basis trading_money NOT NULL,
  gross_realized_pnl trading_money NOT NULL,
  fees trading_money NOT NULL,
  funding trading_money NOT NULL,
  net_realized_pnl trading_money NOT NULL,
  net_deposits trading_money NOT NULL,
  ending_unrealized trading_money,
  ending_equity trading_money,
  reconciliation_difference trading_money NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PROVISIONAL','RECONCILED','QUARANTINED')),
  closing_snapshot_id UUID REFERENCES valuation_snapshots,
  evidence_sha256 TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY(wallet_id,trading_date,revision),
  CHECK(period_start<period_end),
  CHECK(opening_book_balance=opening_cash+opening_option_basis),
  CHECK(ending_book_balance=ending_cash+ending_option_basis),
  CHECK(net_realized_pnl=gross_realized_pnl-fees+funding),
  CHECK(reconciliation_difference=ending_book_balance-opening_book_balance-net_realized_pnl-net_deposits),
  CHECK(status<>'RECONCILED' OR reconciliation_difference=0)
);

CREATE TABLE reconciliation_issues (
  issue_id UUID PRIMARY KEY,
  wallet_id UUID NOT NULL REFERENCES wallets,
  session_id UUID REFERENCES strategy_sessions,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  issue_code TEXT NOT NULL,
  expected_json JSONB NOT NULL,
  actual_json JSONB NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED')),
  detected_at TIMESTAMPTZ NOT NULL,
  resolved_at TIMESTAMPTZ,
  resolution_event_id UUID,
  evidence_json JSONB NOT NULL
);
COMMIT;
