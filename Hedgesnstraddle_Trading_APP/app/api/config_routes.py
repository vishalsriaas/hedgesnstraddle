@router.post("/hedge/strategies")
def update_hedge_strategy_rules(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    strat_id = payload.get("id")
    strat_name = payload.get("strategy_name", "Bullish Hedge")

    strategy = None
    if strat_id:
        strategy = db.query(HedgeStrategyConfig).filter(HedgeStrategyConfig.id == strat_id).first()
    if not strategy:
        strategy = db.query(HedgeStrategyConfig).filter(HedgeStrategyConfig.strategy_name == strat_name).first()

    if not strategy:
        strategy = HedgeStrategyConfig(
            strategy_name=strat_name,
            strategy_key=strat_name.lower().replace(" ", "_")
        )
        db.add(strategy)

    if "enabled" in payload: strategy.enabled = bool(payload["enabled"])
    if "direction" in payload: strategy.direction = str(payload["direction"])
    if "trade_start_h" in payload: strategy.trade_start_h = int(payload["trade_start_h"])
    if "trade_start_m" in payload: strategy.trade_start_m = int(payload["trade_start_m"])
    if "trade_end_h" in payload: strategy.trade_end_h = int(payload["trade_end_h"])
    if "trade_end_m" in payload: strategy.trade_end_m = int(payload["trade_end_m"])
    if "force_close_h" in payload: strategy.force_close_h = int(payload["force_close_h"])
    if "force_close_m" in payload: strategy.force_close_m = int(payload["force_close_m"])
    if "contract_qty" in payload: strategy.contract_qty = float(payload["contract_qty"])
    if "max_premium" in payload: strategy.max_premium = float(payload["max_premium"])
    if "max_time_value" in payload: strategy.max_time_value = float(payload["max_time_value"])

    db.commit()
    return {"status": "SUCCESS", "message": f"Strategy rules updated for '{strategy.strategy_name}'"}
