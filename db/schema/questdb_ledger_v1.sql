-- QuestDB V2 bot-ledger schema reset
-- This file is destructive for development/local setup
-- It drops and recreates QuestDB ledger tables
-- Do not run against data you need to preserve
-- QuestDB is the bot ledger and black-box recorder only
-- Historical market truth remains official Databento DBN or Parquet files outside QuestDB

DROP TABLE IF EXISTS raw_trades;
DROP TABLE IF EXISTS raw_mbp10;
DROP TABLE IF EXISTS raw_ohlcv;
DROP TABLE IF EXISTS raw_bbo;
DROP TABLE IF EXISTS raw_tbbo;
DROP TABLE IF EXISTS databento_definitions;
DROP TABLE IF EXISTS eia_events;
DROP TABLE IF EXISTS sec_events;
DROP TABLE IF EXISTS usaspending_events;
DROP TABLE IF EXISTS macro_timeseries;
DROP TABLE IF EXISTS calendar_events;
DROP TABLE IF EXISTS system_health;
DROP TABLE IF EXISTS ingestion_events;
DROP TABLE IF EXISTS archive_manifest;
DROP TABLE IF EXISTS paper_orders;
DROP TABLE IF EXISTS paper_fills;
DROP TABLE IF EXISTS test_questdb_setup;

DROP TABLE IF EXISTS bot_runs;
DROP TABLE IF EXISTS bot_sessions;
DROP TABLE IF EXISTS feature_snapshots;
DROP TABLE IF EXISTS model_signals;
DROP TABLE IF EXISTS cost_estimates;
DROP TABLE IF EXISTS context_state_snapshots;
DROP TABLE IF EXISTS risk_decisions;
DROP TABLE IF EXISTS context_indicator_snapshots;
DROP TABLE IF EXISTS context_ai_events;
DROP TABLE IF EXISTS context_flags;
DROP TABLE IF EXISTS context_classification_attempts;
DROP TABLE IF EXISTS shadow_context_policy_evaluations;
DROP TABLE IF EXISTS order_events;
DROP TABLE IF EXISTS fill_events;
DROP TABLE IF EXISTS trade_outcomes;
DROP TABLE IF EXISTS latency_metrics;
DROP TABLE IF EXISTS system_health_events;
DROP TABLE IF EXISTS ledger_write_errors;
DROP TABLE IF EXISTS jsonl_fallback_events;

CREATE TABLE bot_runs (
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    run_id STRING,
    session_id STRING,
    environment SYMBOL,
    mode SYMBOL,
    paper_trading BOOLEAN,
    git_commit STRING,
    config_hash STRING,
    status SYMBOL,
    message STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(started_at) PARTITION BY MONTH;

CREATE TABLE bot_sessions (
    session_start_time TIMESTAMP,
    session_end_time TIMESTAMP,
    session_id STRING,
    run_id STRING,
    machine_name SYMBOL,
    service_name SYMBOL,
    environment SYMBOL,
    mode SYMBOL,
    ntp_status SYMBOL,
    clock_offset_ms DOUBLE,
    status SYMBOL,
    message STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(session_start_time) PARTITION BY MONTH;

CREATE TABLE feature_snapshots (
    snapshot_time TIMESTAMP,
    write_time TIMESTAMP,
    feature_snapshot_id STRING,
    ticker SYMBOL,
    feature_version SYMBOL,
    source_record_count INT,
    lookback_window_seconds DOUBLE,
    features_json STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(snapshot_time) PARTITION BY DAY;

CREATE TABLE model_signals (
    signal_time TIMESTAMP,
    write_time TIMESTAMP,
    signal_id STRING,
    ticker SYMBOL,
    signal SYMBOL,
    confidence DOUBLE,
    raw_score DOUBLE,
    model_version SYMBOL,
    calibration_version SYMBOL,
    feature_version SYMBOL,
    feature_snapshot_id STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(signal_time) PARTITION BY DAY;

CREATE TABLE cost_estimates (
    estimate_time TIMESTAMP,
    write_time TIMESTAMP,
    cost_estimate_id STRING,
    ticker SYMBOL,
    signal_id STRING,
    feature_snapshot_id STRING,
    side SYMBOL,
    horizon SYMBOL,
    order_style SYMBOL,
    quantity DOUBLE,
    midprice DOUBLE,
    spread_bps DOUBLE,
    expected_gross_move_bps DOUBLE,
    spread_cost_bps DOUBLE,
    estimated_slippage_bps DOUBLE,
    size_penalty_bps DOUBLE,
    base_cost_bps DOUBLE,
    missed_fill_probability DOUBLE,
    pre_missed_fill_net_edge_bps DOUBLE,
    missed_fill_penalty_bps DOUBLE,
    total_cost_bps DOUBLE,
    min_edge_bps DOUBLE,
    net_expected_edge_bps DOUBLE,
    exceeds_min_edge_threshold BOOLEAN,
    profitable_after_costs BOOLEAN,
    assumptions_version SYMBOL,
    reason STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(estimate_time) PARTITION BY DAY;

CREATE TABLE context_state_snapshots (
    snapshot_time TIMESTAMP,
    write_time TIMESTAMP,
    context_snapshot_id STRING,
    ticker SYMBOL,
    sector SYMBOL,
    active_indicator_ids_json STRING,
    active_context_event_ids_json STRING,
    active_context_flag_ids_json STRING,
    context_summary_json STRING,
    highest_severity SYMBOL,
    risk_level SYMBOL,
    valid_until TIMESTAMP,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(snapshot_time) PARTITION BY DAY;

CREATE TABLE risk_decisions (
    decision_time TIMESTAMP,
    write_time TIMESTAMP,
    risk_decision_id STRING,
    ticker SYMBOL,
    model_signal_id STRING,
    cost_estimate_id STRING,
    context_snapshot_id STRING,
    decision SYMBOL,
    approved BOOLEAN,
    risk_version SYMBOL,
    reduce_size_factor DOUBLE,
    reasons_json STRING,
    thresholds_used_json STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(decision_time) PARTITION BY DAY;

CREATE TABLE context_indicator_snapshots (
    snapshot_time TIMESTAMP,
    write_time TIMESTAMP,
    context_indicator_id STRING,
    source SYMBOL,
    ticker_or_sector SYMBOL,
    indicator_name SYMBOL,
    value_json STRING,
    window SYMBOL,
    units SYMBOL,
    freshness_seconds DOUBLE,
    source_event_time TIMESTAMP,
    details_json STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(snapshot_time) PARTITION BY DAY;

CREATE TABLE context_ai_events (
    event_time TIMESTAMP,
    write_time TIMESTAMP,
    context_event_id STRING,
    source SYMBOL,
    source_id STRING,
    affected_tickers_json STRING,
    affected_sector SYMBOL,
    event_type SYMBOL,
    sentiment SYMBOL,
    urgency SYMBOL,
    risk_level SYMBOL,
    confidence DOUBLE,
    valid_from TIMESTAMP,
    valid_until TIMESTAMP,
    summary STRING,
    prompt_version SYMBOL,
    model_version SYMBOL,
    raw_input_hash STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING,
    raw_input_id STRING,
    source_document_id STRING,
    classification_request_id STRING,
    classification_attempt_id STRING,
    validation_result_id STRING,
    source_type SYMBOL,
    source_platform SYMBOL,
    source_uri STRING,
    source_locator STRING,
    document_hash STRING,
    source_published_at TIMESTAMP,
    source_updated_at TIMESTAMP,
    collected_at TIMESTAMP,
    normalized_at TIMESTAMP,
    classified_at TIMESTAMP,
    available_at TIMESTAMP,
    validated_at TIMESTAMP,
    provider SYMBOL
) TIMESTAMP(event_time) PARTITION BY DAY;

CREATE TABLE context_flags (
    event_time TIMESTAMP,
    write_time TIMESTAMP,
    context_flag_id STRING,
    source SYMBOL,
    flag_type SYMBOL,
    severity SYMBOL,
    ticker SYMBOL,
    sector SYMBOL,
    confidence DOUBLE,
    valid_until TIMESTAMP,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING,
    context_event_id STRING,
    raw_input_id STRING,
    source_document_id STRING,
    classification_request_id STRING,
    classification_attempt_id STRING,
    validation_result_id STRING,
    source_type SYMBOL,
    source_id STRING,
    source_platform SYMBOL,
    source_uri STRING,
    source_locator STRING,
    document_hash STRING,
    raw_input_hash STRING,
    valid_from TIMESTAMP,
    available_at TIMESTAMP,
    validated_at TIMESTAMP,
    reason_codes_json STRING,
    summary STRING
) TIMESTAMP(event_time) PARTITION BY DAY;

CREATE TABLE context_classification_attempts (
    requested_at TIMESTAMP,
    write_time TIMESTAMP,
    classification_attempt_id STRING,
    classification_request_id STRING,
    raw_input_id STRING,
    source_document_id STRING,
    source SYMBOL,
    source_type SYMBOL,
    source_platform SYMBOL,
    source_uri STRING,
    source_locator STRING,
    affected_tickers_json STRING,
    raw_input_hash STRING,
    document_hash STRING,
    source_published_at TIMESTAMP,
    source_updated_at TIMESTAMP,
    collected_at TIMESTAMP,
    normalized_at TIMESTAMP,
    classified_at TIMESTAMP,
    provider SYMBOL,
    model_version SYMBOL,
    prompt_version SYMBOL,
    status SYMBOL,
    event_type SYMBOL,
    risk_level SYMBOL,
    urgency SYMBOL,
    confidence DOUBLE,
    summary STRING,
    validation_result_id STRING,
    validation_outcome BOOLEAN,
    validation_reason_codes_json STRING,
    validator_version SYMBOL,
    validated_at TIMESTAMP,
    provider_latency_ms DOUBLE,
    safe_failure_category SYMBOL,
    safe_failure_summary STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING,
    provider_request_count LONG,
    retry_count LONG,
    deduplicated BOOLEAN,
    reused_classification_attempt_id STRING
) TIMESTAMP(requested_at) PARTITION BY DAY;

CREATE TABLE shadow_context_policy_evaluations (
    decision_evaluation_time TIMESTAMP,
    write_time TIMESTAMP,
    shadow_evaluation_id STRING,
    model_signal_id STRING,
    risk_decision_id STRING,
    matched_context_event_ids_json STRING,
    matched_context_flag_ids_json STRING,
    shadow_context_fingerprint STRING,
    policy_version SYMBOL,
    policy_config_hash STRING,
    hypothetical_action SYMBOL,
    proposed_size_factor DOUBLE,
    reason_codes_json STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(decision_evaluation_time) PARTITION BY DAY;

CREATE TABLE order_events (
    order_time TIMESTAMP,
    write_time TIMESTAMP,
    order_id STRING,
    ticker SYMBOL,
    side SYMBOL,
    order_type SYMBOL,
    quantity DOUBLE,
    status SYMBOL,
    expected_price DOUBLE,
    submitted_price DOUBLE,
    broker SYMBOL,
    broker_order_id STRING,
    paper_trading BOOLEAN,
    model_signal_id STRING,
    risk_decision_id STRING,
    feature_snapshot_id STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(order_time) PARTITION BY DAY;

CREATE TABLE fill_events (
    fill_time TIMESTAMP,
    write_time TIMESTAMP,
    fill_id STRING,
    order_id STRING,
    ticker SYMBOL,
    side SYMBOL,
    quantity DOUBLE,
    fill_price DOUBLE,
    expected_price DOUBLE,
    slippage DOUBLE,
    slippage_bps DOUBLE,
    broker_status SYMBOL,
    broker_fill_id STRING,
    model_signal_id STRING,
    risk_decision_id STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(fill_time) PARTITION BY DAY;

CREATE TABLE trade_outcomes (
    entry_time TIMESTAMP,
    write_time TIMESTAMP,
    outcome_id STRING,
    signal_id STRING,
    order_id STRING,
    fill_id STRING,
    ticker SYMBOL,
    exit_time TIMESTAMP,
    entry_price DOUBLE,
    exit_price DOUBLE,
    quantity DOUBLE,
    realized_pnl DOUBLE,
    return_1m DOUBLE,
    return_5m DOUBLE,
    return_15m DOUBLE,
    max_favorable_excursion DOUBLE,
    max_adverse_excursion DOUBLE,
    result SYMBOL,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(entry_time) PARTITION BY DAY;

CREATE TABLE latency_metrics (
    measured_time TIMESTAMP,
    write_time TIMESTAMP,
    latency_metric_id STRING,
    component SYMBOL,
    source SYMBOL,
    latency_ms DOUBLE,
    ticker SYMBOL,
    event_type SYMBOL,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(measured_time) PARTITION BY DAY;

CREATE TABLE system_health_events (
    event_time TIMESTAMP,
    write_time TIMESTAMP,
    health_event_id STRING,
    component SYMBOL,
    status SYMBOL,
    message STRING,
    cpu_percent DOUBLE,
    memory_percent DOUBLE,
    clock_offset_ms DOUBLE,
    feed_delay_ms DOUBLE,
    reconnect_count INT,
    queue_depth INT,
    ledger_write_errors INT,
    jsonl_fallback_count INT,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(event_time) PARTITION BY DAY;

CREATE TABLE ledger_write_errors (
    event_time TIMESTAMP,
    write_time TIMESTAMP,
    error_id STRING,
    target_table SYMBOL,
    component SYMBOL,
    severity SYMBOL,
    record_type SYMBOL,
    record_id STRING,
    error_message STRING,
    payload_json STRING,
    jsonl_fallback_path STRING,
    fallback_written BOOLEAN,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(event_time) PARTITION BY DAY;

CREATE TABLE jsonl_fallback_events (
    event_time TIMESTAMP,
    write_time TIMESTAMP,
    fallback_event_id STRING,
    component SYMBOL,
    target_table SYMBOL,
    record_type SYMBOL,
    record_id STRING,
    file_path STRING,
    bytes_written LONG,
    status SYMBOL,
    message STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(event_time) PARTITION BY DAY;
