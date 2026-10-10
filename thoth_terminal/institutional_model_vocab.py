from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import SourceActor, SourceActorAlias
from thoth_terminal.source_identity_service import (
    ensure_source_actor,
    ensure_source_actor_alias,
    find_source_actor_by_alias,
    normalize_actor_type,
    normalize_side,
    normalize_source_alias,
)


MODEL_VOCAB_VERSION = "institutional-model-v2"
SOURCE_REGISTRY_VERSION = "institutional-source-registry-v1"


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    label: str
    value_type: str
    description: str
    aliases: tuple[str, ...] = ()
    unit_examples: tuple[str, ...] = ()
    state_vector_field: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    period_required: bool = False


MODEL_METRICS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        key="revenue",
        label="Revenue",
        value_type="money",
        description="Revenue or sales estimate/actual.",
        aliases=("sales", "turnover"),
        unit_examples=("USD", "JPY", "KRW"),
        period_required=True,
    ),
    MetricDefinition(
        key="eps",
        label="EPS",
        value_type="money",
        description="Earnings per share estimate or actual.",
        aliases=("earnings per share", "diluted eps"),
        unit_examples=("USD/share",),
        period_required=True,
    ),
    MetricDefinition(
        key="gross_profit",
        label="Gross Profit",
        value_type="money",
        description="Gross profit estimate or actual.",
        aliases=("gross income",),
        unit_examples=("USD", "JPY", "KRW"),
        period_required=True,
    ),
    MetricDefinition(
        key="operating_income",
        label="Operating Income",
        value_type="money",
        description="Operating income, operating profit, or EBIT estimate/actual.",
        aliases=("operating profit", "ebit"),
        unit_examples=("USD", "JPY", "KRW"),
        period_required=True,
    ),
    MetricDefinition(
        key="net_income",
        label="Net Income",
        value_type="money",
        description="Net income, net profit, or earnings estimate/actual.",
        aliases=("net profit", "earnings", "profit"),
        unit_examples=("USD", "JPY", "KRW"),
        period_required=True,
    ),
    MetricDefinition(
        key="ebitda",
        label="EBITDA",
        value_type="money",
        description="EBITDA estimate or actual.",
        aliases=("adjusted ebitda",),
        unit_examples=("USD", "JPY", "KRW"),
        period_required=True,
    ),
    MetricDefinition(
        key="free_cash_flow",
        label="Free Cash Flow",
        value_type="money",
        description="Free cash flow estimate, actual, or guide.",
        aliases=("fcf", "surplus cash flow"),
        unit_examples=("USD", "JPY", "KRW"),
        period_required=True,
    ),
    MetricDefinition(
        key="gross_margin",
        label="Gross Margin",
        value_type="percent_bounded",
        description="Gross margin estimate or actual.",
        aliases=("gm", "gross margin percent"),
        unit_examples=("%",),
        min_value=0,
        max_value=100,
        period_required=True,
    ),
    MetricDefinition(
        key="operating_margin",
        label="Operating Margin",
        value_type="percent_bounded",
        description="Operating margin estimate or actual.",
        aliases=("op margin", "operating margin percent"),
        unit_examples=("%",),
        min_value=0,
        max_value=100,
        period_required=True,
    ),
    MetricDefinition(
        key="capex",
        label="Capex",
        value_type="money",
        description="Capital expenditure or capacity investment.",
        aliases=("capital expenditure", "capital spending", "investment"),
        unit_examples=("USD", "JPY", "KRW"),
        period_required=True,
    ),
    MetricDefinition(
        key="bookings",
        label="Bookings",
        value_type="money",
        description="Order intake, bookings, or new orders for a stated period.",
        aliases=("orders", "order intake", "new orders", "booked orders"),
        unit_examples=("USD", "JPY", "KRW"),
        period_required=True,
    ),
    MetricDefinition(
        key="backlog",
        label="Backlog",
        value_type="money",
        description="Order backlog, remaining performance obligation, or contracted revenue not yet recognized.",
        aliases=("order backlog", "rpo", "remaining performance obligation", "contracted backlog"),
        unit_examples=("USD", "JPY", "KRW"),
        period_required=True,
    ),
    MetricDefinition(
        key="tam",
        label="TAM",
        value_type="money",
        description="Total addressable market estimate.",
        aliases=("total addressable market",),
        unit_examples=("USD",),
        period_required=True,
    ),
    MetricDefinition(
        key="sam",
        label="SAM",
        value_type="money",
        description="Serviceable addressable market estimate.",
        aliases=("serviceable addressable market",),
        unit_examples=("USD",),
        period_required=True,
    ),
    MetricDefinition(
        key="som",
        label="SOM",
        value_type="money",
        description="Serviceable obtainable market estimate.",
        aliases=("serviceable obtainable market",),
        unit_examples=("USD",),
        period_required=True,
    ),
    MetricDefinition(
        key="installed_capacity",
        label="Installed Capacity",
        value_type="quantity",
        description="Nameplate or installed physical capacity.",
        aliases=("nameplate capacity", "capacity installed", "installed supply"),
        unit_examples=("wafers/month", "tons/year", "MW", "units/year"),
        state_vector_field="installed_capacity",
        period_required=True,
    ),
    MetricDefinition(
        key="nameplate_capacity",
        label="Nameplate Capacity",
        value_type="quantity",
        description="Designed or rated facility capacity before utilization, qualification, or uptime adjustments.",
        aliases=("rated capacity", "design capacity", "nominal capacity"),
        unit_examples=("wafers/month", "tons/year", "MW", "units/year"),
        state_vector_field="installed_capacity",
        period_required=True,
    ),
    MetricDefinition(
        key="current_capacity",
        label="Current Capacity",
        value_type="quantity",
        description="Currently available, online, or operating capacity at an existing facility or project phase.",
        aliases=("available capacity", "online capacity", "running capacity", "current output capacity"),
        unit_examples=("wafers/month", "tons/year", "MW", "units/year"),
        state_vector_field="effective_capacity",
        period_required=True,
    ),
    MetricDefinition(
        key="effective_capacity",
        label="Effective Capacity",
        value_type="quantity",
        description="Usable capacity after yield, qualification, or uptime constraints.",
        aliases=("usable capacity", "qualified capacity", "available capacity"),
        unit_examples=("wafers/month", "tons/year", "MW", "units/year"),
        state_vector_field="effective_capacity",
        period_required=True,
    ),
    MetricDefinition(
        key="capacity_addition",
        label="Capacity Addition",
        value_type="quantity",
        description="Incremental capacity expected or added in a period.",
        aliases=("capacity expansion", "new capacity", "capacity ramp"),
        unit_examples=("wafers/month", "tons/year", "MW", "units/year"),
        period_required=True,
    ),
    MetricDefinition(
        key="utilization",
        label="Utilization",
        value_type="percent_bounded",
        description="Nonnegative utilization rate capped at 100.",
        aliases=("utilisation", "utilization rate", "fab utilization"),
        unit_examples=("%",),
        state_vector_field="utilization",
        min_value=0,
        max_value=100,
        period_required=True,
    ),
    MetricDefinition(
        key="yield_rate",
        label="Yield",
        value_type="percent_bounded",
        description="Process, production, or qualification yield capped at 100.",
        aliases=("yield", "production yield", "device yield"),
        unit_examples=("%",),
        state_vector_field="yield_rate",
        min_value=0,
        max_value=100,
        period_required=True,
    ),
    MetricDefinition(
        key="market_share",
        label="Market Share",
        value_type="percent_bounded",
        description="Share of a market, customer, product, or segment; cannot be negative or exceed 100.",
        aliases=("share", "market share percent", "share of market"),
        unit_examples=("%",),
        min_value=0,
        max_value=100,
        period_required=True,
    ),
    MetricDefinition(
        key="price_target",
        label="Price Target",
        value_type="money",
        description="Source-owned equity price target or target-price change.",
        aliases=("target price", "pt", "price target change", "target-price"),
        unit_examples=("USD/share", "JPY/share", "KRW/share"),
        period_required=False,
    ),
    MetricDefinition(
        key="analyst_rating",
        label="Analyst Rating",
        value_type="status",
        description="Source-owned analyst stock rating, recommendation, or rating change.",
        aliases=("rating", "stock rating", "recommendation", "buy rating", "rating change"),
        period_required=False,
    ),
    MetricDefinition(
        key="lead_time_days",
        label="Lead Time",
        value_type="days",
        description="Lead time expressed in days.",
        aliases=("lead time", "delivery lead time", "queue time"),
        unit_examples=("days",),
        state_vector_field="lead_time_days",
        period_required=False,
    ),
    MetricDefinition(
        key="lead_time_weeks",
        label="Lead Time Weeks",
        value_type="weeks",
        description="Lead time expressed in weeks; state vectors convert to days.",
        aliases=("lead time weeks", "delivery lead time weeks"),
        unit_examples=("weeks",),
        state_vector_field="lead_time_days",
        period_required=False,
    ),
    MetricDefinition(
        key="inventory_days",
        label="Inventory Days",
        value_type="days",
        description="Inventory buffer in days.",
        aliases=("days inventory", "inventory buffer", "doi"),
        unit_examples=("days",),
        state_vector_field="inventory_days",
        period_required=False,
    ),
    MetricDefinition(
        key="allocation_status",
        label="Allocation Status",
        value_type="status",
        description="Allocation posture such as open, constrained, tight, or fully allocated.",
        aliases=("allocation", "allocated", "supply allocation"),
        state_vector_field="allocation_status",
    ),
    MetricDefinition(
        key="qualification_status",
        label="Qualification Status",
        value_type="status",
        description="Customer, product, or process qualification status.",
        aliases=("qualification", "qualified", "customer qualification"),
        state_vector_field="qualification_status",
    ),
    MetricDefinition(
        key="demand_signal",
        label="Demand Signal",
        value_type="status",
        description="Directional demand status such as accelerating, stable, softening, or cancelled.",
        aliases=("demand", "order signal", "backlog signal"),
        state_vector_field="demand_signal",
    ),
    MetricDefinition(
        key="schedule_status",
        label="Schedule Status",
        value_type="status",
        description="Schedule, ramp, or delivery status.",
        aliases=("schedule", "ramp status", "timeline status"),
        state_vector_field="schedule_status",
    ),
    MetricDefinition(
        key="logistics_friction",
        label="Logistics Friction",
        value_type="status",
        description="Shipping, permitting, procurement, or logistics friction.",
        aliases=("logistics", "procurement friction", "shipping friction"),
        state_vector_field="logistics_friction",
    ),
    MetricDefinition(
        key="token_usage",
        label="Token Usage",
        value_type="quantity",
        description="AI token usage through a stated time period.",
        aliases=("tokens", "token consumption", "inference tokens"),
        unit_examples=("tokens/day", "tokens/month", "tokens/year"),
        period_required=True,
    ),
    MetricDefinition(
        key="hbm_wafer_capacity_wpm",
        label="HBM Wafer Capacity",
        value_type="quantity",
        description="Front-end wafer capacity allocated to HBM, usually expressed as wafers per month.",
        aliases=("hbm wafer capacity", "hbm frontend wafer capacity", "hbm wpm", "hbm front-end wpm"),
        unit_examples=("WPM", "wafers/month"),
        state_vector_field="installed_capacity",
        period_required=True,
    ),
    MetricDefinition(
        key="cowos_capacity_wpm",
        label="CoWoS Capacity",
        value_type="quantity",
        description="CoWoS or advanced packaging wafer capacity by provider, customer, segment, or scenario.",
        aliases=("cowos capacity", "cowos wpm", "cowos-s capacity", "cowos-r capacity", "advanced packaging wpm"),
        unit_examples=("WPM", "wafers/month"),
        state_vector_field="installed_capacity",
        period_required=True,
    ),
    MetricDefinition(
        key="osat_packaging_capacity_wpm",
        label="OSAT Packaging Capacity",
        value_type="quantity",
        description="OSAT or subcontracted advanced-packaging capacity such as ASE or Amkor WPM.",
        aliases=("osat capacity", "ase capacity", "amkor capacity", "outsourced packaging capacity"),
        unit_examples=("WPM", "wafers/month"),
        state_vector_field="installed_capacity",
        period_required=True,
    ),
    MetricDefinition(
        key="ai_gpu_production_units",
        label="AI GPU Production Units",
        value_type="unit_count",
        description="AI GPU or accelerator production units for a period, often split by product family.",
        aliases=("ai gpu production", "gpu production units", "accelerator production", "ai accelerator units"),
        unit_examples=("units", "mn units", "accelerators"),
        period_required=True,
    ),
    MetricDefinition(
        key="product_unit_mix",
        label="Product Unit Mix",
        value_type="unit_count",
        description="Production-unit split by product family, generation, customer, or package.",
        aliases=("unit split", "product unit split", "blackwell units", "rubin units", "hopper units"),
        unit_examples=("units", "mn units"),
        period_required=True,
    ),
    MetricDefinition(
        key="technology_bit_mix_share",
        label="Technology Bit Mix Share",
        value_type="percent_bounded",
        description="Share of DRAM/NAND/HBM bit output by technology generation or node.",
        aliases=("bit mix", "technology mix", "1-gamma bit mix", "g9 nand bit mix"),
        unit_examples=("%",),
        min_value=0,
        max_value=100,
        period_required=True,
    ),
    MetricDefinition(
        key="supply_undersupply_until",
        label="Undersupply Until",
        value_type="status",
        description="Source-owned forecast for how long a market remains undersupplied or oversupplied.",
        aliases=("undersupplied until", "undersupply until", "supply tight until", "shortage until"),
        state_vector_field="allocation_status",
        period_required=True,
    ),
    MetricDefinition(
        key="facility_ramp_schedule",
        label="Facility Ramp Schedule",
        value_type="status",
        description="Fab/site/project construction, qualification, or ramp timing and schedule status.",
        aliases=("facility ramp", "fab ramp", "site ramp", "project timing", "facility timing", "ramp schedule"),
        state_vector_field="schedule_status",
        period_required=False,
    ),
    MetricDefinition(
        key="data_ssd_share",
        label="Data Center SSD Share",
        value_type="percent_bounded",
        description="Data-center SSD or enterprise SSD share of NAND, SSD, or company sales mix.",
        aliases=("data ssd share", "enterprise ssd share", "data center ssd share"),
        unit_examples=("%",),
        min_value=0,
        max_value=100,
        period_required=True,
    ),
    MetricDefinition(
        key="online_cluster_capacity_gpus",
        label="Online Cluster Capacity",
        value_type="unit_count",
        description="Schedulable online AI cluster capacity in GPUs, accelerators, racks, or equivalent units.",
        aliases=("online gpu capacity", "cluster capacity", "schedulable capacity", "gpu-online count", "online accelerators"),
        unit_examples=("GPUs", "accelerators", "racks"),
        state_vector_field="installed_capacity",
        period_required=True,
    ),
    MetricDefinition(
        key="cloud_committed_spend",
        label="Cloud Committed Spend",
        value_type="money",
        description="Cloud or AI infrastructure committed spend, take-or-pay amount, backlog, or capacity commitment.",
        aliases=("cloud commitment", "committed spend", "take-or-pay", "cloud backlog", "capacity commitment"),
        unit_examples=("USD",),
        period_required=True,
    ),
    MetricDefinition(
        key="cloud_capacity_utilization",
        label="Cloud Capacity Utilization",
        value_type="percent_bounded",
        description="Utilization of AI cloud, GPU cluster, cloud SKU, or committed capacity.",
        aliases=("cloud utilization", "gpu cloud utilization", "capacity utilization"),
        unit_examples=("%",),
        state_vector_field="utilization",
        min_value=0,
        max_value=100,
        period_required=True,
    ),
    MetricDefinition(
        key="model_context_window_tokens",
        label="Model Context Window",
        value_type="quantity",
        description="Model context length or effective usable context window.",
        aliases=("context window", "context length", "max context", "sequence length"),
        unit_examples=("tokens",),
        period_required=False,
    ),
    MetricDefinition(
        key="benchmark_score",
        label="Benchmark Score",
        value_type="quantity",
        description="AI model, EDA, runtime, cyber, or system benchmark result with named benchmark and scenario.",
        aliases=("benchmark", "eval score", "accuracy score", "performance score"),
        unit_examples=("score", "%", "tokens/s"),
        period_required=False,
    ),
    MetricDefinition(
        key="vector_index_volume",
        label="Vector Index Volume",
        value_type="quantity",
        description="Indexed corpus, vector count, document count, embedding volume, or search index size.",
        aliases=("vector volume", "indexed corpus", "indexed documents", "embedding volume", "vector count"),
        unit_examples=("vectors", "documents", "GB"),
        period_required=True,
    ),
    MetricDefinition(
        key="rag_query_volume",
        label="RAG Query Volume",
        value_type="quantity",
        description="RAG, enterprise-search, retrieval, or vector-search query/request volume.",
        aliases=("rag queries", "retrieval queries", "enterprise search queries", "vector search qps"),
        unit_examples=("queries/day", "QPS", "requests/month"),
        period_required=True,
    ),
    MetricDefinition(
        key="eval_observability_trace_volume",
        label="Eval/Observability Trace Volume",
        value_type="quantity",
        description="Model evaluation, observability, tracing, guardrail, or monitoring usage volume.",
        aliases=("eval volume", "trace volume", "model traces", "guardrail checks", "observability traces"),
        unit_examples=("runs", "traces", "checks", "events"),
        period_required=True,
    ),
    MetricDefinition(
        key="governed_model_count",
        label="Governed Model Count",
        value_type="unit_count",
        description="Number of models, apps, agents, or workflows covered by governance, compliance, or model-risk controls.",
        aliases=("governed models", "controlled models", "approved models", "model risk count"),
        unit_examples=("models", "apps", "workflows"),
        period_required=True,
    ),
    MetricDefinition(
        key="cloud_incident_capacity_impact",
        label="Cloud Incident Capacity Impact",
        value_type="quantity",
        description="Capacity, service, customer, revenue, or availability impact from a cloud/data-center incident or attack.",
        aliases=("incident capacity impact", "cloud incident impact", "data center attack impact", "outage capacity impact"),
        unit_examples=("GPUs", "MW", "customers", "USD"),
        period_required=False,
    ),
    MetricDefinition(
        key="outage_duration_hours",
        label="Outage Duration",
        value_type="quantity",
        description="Cloud, data-center, network, cyber, or operational outage duration.",
        aliases=("outage duration", "downtime", "incident duration"),
        unit_examples=("hours", "days"),
        period_required=False,
    ),
    MetricDefinition(
        key="shipment_units",
        label="Shipment Units",
        value_type="unit_count",
        description="Shipment, delivery, build, or arrival quantity for a product, component, rack, tool, or system.",
        aliases=("shipments", "units shipped", "deliveries", "arrival units", "shipment count"),
        unit_examples=("units", "racks", "tools", "modules"),
        period_required=True,
    ),
    MetricDefinition(
        key="customs_hold_status",
        label="Customs Hold Status",
        value_type="status",
        description="Customs, export-license, declaration, clearance, hold, release, or border-delay state.",
        aliases=("customs hold", "customs release", "export license status", "clearance status"),
        state_vector_field="logistics_friction",
        period_required=False,
    ),
    MetricDefinition(
        key="component_lot_status",
        label="Component Lot Status",
        value_type="status",
        description="Lot, batch, serial/date-code, certificate, quarantine, counterfeit, RMA, or disposition status.",
        aliases=("lot status", "batch status", "serial traceability", "component provenance", "quality traceability"),
        state_vector_field="qualification_status",
        period_required=False,
    ),
    MetricDefinition(
        key="supplier_site_exposure_share",
        label="Supplier Site Exposure Share",
        value_type="percent_bounded",
        description="Share of sourcing, capacity, output, spend, or dependency exposed to a supplier site, lane, chokepoint, or region.",
        aliases=("site exposure", "route exposure", "supplier dependency share", "site dependency share"),
        unit_examples=("%",),
        min_value=0,
        max_value=100,
        period_required=True,
    ),
    MetricDefinition(
        key="transit_time_days",
        label="Transit Time",
        value_type="days",
        description="Freight, lane, customs, staging, warehouse, or install transit/dwell time.",
        aliases=("transit time", "dwell time", "shipping delay", "warehouse dwell"),
        unit_examples=("days",),
        state_vector_field="logistics_friction",
        period_required=False,
    ),
    MetricDefinition(
        key="vulnerability_count",
        label="Vulnerability Count",
        value_type="unit_count",
        description="Cybersecurity vulnerability, exploit, incident, severity, or finding count.",
        aliases=("vulnerabilities", "severe vulnerabilities", "high vulnerabilities", "cve count", "finding count"),
        unit_examples=("count", "CVEs", "findings"),
        period_required=True,
    ),
    MetricDefinition(
        key="qualified_package_output_units",
        label="Qualified Package Output",
        value_type="unit_count",
        description="Qualified advanced-packaging output after interposer/package geometry, yield, cycle-time, and customer qualification constraints.",
        aliases=("qualified packages", "package output", "cowos package output", "qualified cowos output"),
        unit_examples=("packages", "modules", "accelerators"),
        state_vector_field="effective_capacity",
        period_required=True,
    ),
    MetricDefinition(
        key="interposer_area_mm2",
        label="Interposer Area",
        value_type="quantity",
        description="Interposer or package substrate area used to convert wafer starts into package output.",
        aliases=("interposer area", "package area", "reticle area", "interposer size"),
        unit_examples=("mm2", "cm2"),
        period_required=False,
    ),
    MetricDefinition(
        key="cycle_time_days",
        label="Cycle Time",
        value_type="days",
        description="Fab, packaging, burn-in, test, customs, deployment, or qualification cycle time.",
        aliases=("cycle time", "turnaround time", "manufacturing cycle time", "test cycle time"),
        unit_examples=("days", "weeks"),
        state_vector_field="lead_time_days",
        period_required=False,
    ),
    MetricDefinition(
        key="known_good_die_yield",
        label="Known-Good-Die Yield",
        value_type="percent_bounded",
        description="Known-good-die, stack, package, burn-in, or first-pass yield used in bottleneck conversion math.",
        aliases=("kgd yield", "stack yield", "package yield", "first-pass yield", "burn-in yield"),
        unit_examples=("%",),
        min_value=0,
        max_value=100,
        state_vector_field="yield_rate",
        period_required=True,
    ),
    MetricDefinition(
        key="dies_per_stack",
        label="Dies Per Stack",
        value_type="quantity",
        description="HBM, chiplet, or package die count per stack/module used to convert die supply into stack or accelerator output.",
        aliases=("dies per stack", "die count per stack", "stack height", "hbm stack height"),
        unit_examples=("dies/stack", "hi"),
        period_required=False,
    ),
    MetricDefinition(
        key="stacks_per_accelerator",
        label="Stacks Per Accelerator",
        value_type="quantity",
        description="HBM stack count or memory-package attachment count required per accelerator, module, or board.",
        aliases=("stacks per gpu", "hbm stacks per accelerator", "memory stacks per accelerator"),
        unit_examples=("stacks/GPU", "stacks/accelerator"),
        period_required=False,
    ),
    MetricDefinition(
        key="rack_output_units",
        label="Rack Output",
        value_type="unit_count",
        description="Qualified rack, tray, board, server, or AI system output after integration, burn-in, validation, and rework.",
        aliases=("rack output", "system output", "qualified rack output", "ai rack shipments"),
        unit_examples=("racks", "systems", "boards", "trays"),
        state_vector_field="effective_capacity",
        period_required=True,
    ),
    MetricDefinition(
        key="site_acceptance_status",
        label="Site Acceptance Status",
        value_type="status",
        description="Delivered, installed, commissioned, accepted, online, or schedulable deployment status for AI factory capacity.",
        aliases=("site acceptance", "commissioning status", "deployment status", "schedulable status"),
        state_vector_field="qualification_status",
        period_required=False,
    ),
    MetricDefinition(
        key="generation_equivalent_compute_units",
        label="Generation-Equivalent Compute",
        value_type="quantity",
        description="Normalized accelerator fleet or supply converted across generations using performance, memory, power, and utilization equivalence.",
        aliases=("generation equivalent", "h100 equivalent", "b200 equivalent", "accelerator equivalent supply"),
        unit_examples=("H100e", "B200e", "accelerator-equivalent"),
        state_vector_field="effective_capacity",
        period_required=True,
    ),
    MetricDefinition(
        key="power_derate_percent",
        label="Power Derate",
        value_type="percent_bounded",
        description="Capacity derate from power, cooling, uptime, allocation, or deployment limits.",
        aliases=("derate", "power derate", "cooling derate", "capacity derate"),
        unit_examples=("%",),
        min_value=0,
        max_value=100,
        state_vector_field="utilization",
        period_required=False,
    ),
    MetricDefinition(
        key="asp",
        label="ASP",
        value_type="money",
        description="Average selling price.",
        aliases=("average selling price", "price per unit"),
        unit_examples=("USD/unit",),
        period_required=True,
    ),
    MetricDefinition(
        key="pricing",
        label="Pricing",
        value_type="money",
        description="Price, cost, fee, or contract price.",
        aliases=("price", "cost", "contract price"),
        unit_examples=("USD", "USD/unit"),
        period_required=True,
    ),
    MetricDefinition(
        key="growth_rate",
        label="Growth Rate",
        value_type="percent_unbounded",
        description="YoY, QoQ, CAGR, or other growth/change percentage that may be negative or exceed 100.",
        aliases=("growth", "yoy", "qoq", "cagr", "increase", "decrease"),
        unit_examples=("%",),
        period_required=True,
    ),
)


MODEL_SIGNAL_RAILS: tuple[dict[str, Any], ...] = (
    {
        "key": "source_owned_consensus",
        "destination": "analyst_evidence_and_institutional_observation",
        "fact_classes": ["consensus", "estimate", "guidance", "model_delta", "rating", "price_target"],
        "metric_keys": [
            "revenue",
            "gross_profit",
            "operating_income",
            "net_income",
            "eps",
            "ebitda",
            "free_cash_flow",
            "gross_margin",
            "operating_margin",
            "capex",
            "bookings",
            "backlog",
            "market_share",
            "price_target",
            "analyst_rating",
            "growth_rate",
        ],
        "required_fields": ["estimate_owner", "metric_key", "value_or_status", "period", "source_document_id"],
    },
    {
        "key": "memory_capacity_supply",
        "destination": "analyst_evidence_and_bottleneck_inputs",
        "fact_classes": ["capacity", "supply_demand", "technology_mix", "stack_conversion"],
        "metric_keys": ["hbm_wafer_capacity_wpm", "known_good_die_yield", "dies_per_stack", "stacks_per_accelerator", "technology_bit_mix_share", "data_ssd_share", "supply_undersupply_until"],
        "required_fields": ["entity_or_market", "product_family", "value_or_status", "period", "source_owner"],
    },
    {
        "key": "advanced_packaging_capacity",
        "destination": "analyst_evidence_supply_chain_bottleneck",
        "fact_classes": ["capacity", "customer_share", "bottleneck_clearing", "package_conversion"],
        "metric_keys": ["cowos_capacity_wpm", "osat_packaging_capacity_wpm", "qualified_package_output_units", "interposer_area_mm2", "cycle_time_days", "known_good_die_yield", "market_share"],
        "required_fields": ["provider_or_customer", "capacity_type", "value_or_status", "period", "source_owner"],
    },
    {
        "key": "accelerator_production_roadmap",
        "destination": "analyst_evidence_product_stage_company_page",
        "fact_classes": ["production", "roadmap", "customer_order", "system_integration", "fleet_equivalence"],
        "metric_keys": ["ai_gpu_production_units", "product_unit_mix", "rack_output_units", "generation_equivalent_compute_units", "shipment_units", "schedule_status"],
        "required_fields": ["product_family", "value_or_status", "period", "source_owner"],
    },
    {
        "key": "facility_capex_ramp",
        "destination": "analyst_evidence_dataset_supply_chain",
        "fact_classes": ["capex", "facility_timing", "capacity_addition", "construction_stage"],
        "metric_keys": ["capex", "facility_ramp_schedule", "capacity_addition", "installed_capacity", "nameplate_capacity", "current_capacity", "effective_capacity", "utilization"],
        "required_fields": ["facility_or_project", "purpose", "value_or_status", "period", "source_owner"],
    },
    {
        "key": "cloud_capacity_economics",
        "destination": "analyst_evidence_dataset_company_page",
        "fact_classes": ["cloud_capacity", "committed_spend", "utilization", "online_capacity", "site_acceptance"],
        "metric_keys": ["online_cluster_capacity_gpus", "site_acceptance_status", "generation_equivalent_compute_units", "power_derate_percent", "cloud_committed_spend", "cloud_capacity_utilization"],
        "required_fields": ["cloud_provider_or_customer", "capacity_unit", "value_or_status", "period", "source_owner"],
    },
    {
        "key": "ai_workload_and_runtime",
        "destination": "analyst_evidence_dataset_bottleneck_input",
        "fact_classes": ["workload_usage", "runtime", "model_performance", "compute_conversion"],
        "metric_keys": ["token_usage", "model_context_window_tokens", "benchmark_score"],
        "required_fields": ["workload_or_model", "metric_key", "value_or_status", "period_or_as_of", "source_owner"],
    },
    {
        "key": "ai_data_platform_rag",
        "destination": "analyst_evidence_dataset_company_page",
        "fact_classes": ["rag", "vector_search", "enterprise_search", "data_platform"],
        "metric_keys": ["vector_index_volume", "rag_query_volume", "revenue", "cloud_capacity_utilization"],
        "required_fields": ["vendor_or_customer", "workload", "value_or_status", "period", "source_owner"],
    },
    {
        "key": "ai_evalops_observability_governance",
        "destination": "analyst_evidence_dataset_research_request_if_missing",
        "fact_classes": ["evalops", "observability", "guardrail", "governance", "model_risk"],
        "metric_keys": ["eval_observability_trace_volume", "governed_model_count", "benchmark_score"],
        "required_fields": ["vendor_or_program", "control_or_eval_type", "value_or_status", "period_or_as_of", "source_owner"],
    },
    {
        "key": "cloud_datacenter_incident",
        "destination": "analyst_evidence_catalyst_incident_rail",
        "fact_classes": ["incident", "attack", "outage", "capacity_impact"],
        "metric_keys": ["cloud_incident_capacity_impact", "outage_duration_hours", "online_cluster_capacity_gpus"],
        "required_fields": ["operator_or_site", "incident_type", "impact_value_or_status", "time_window", "source_owner"],
    },
    {
        "key": "logistics_customs_traceability",
        "destination": "analyst_evidence_supply_chain_dataset",
        "fact_classes": ["shipment", "customs", "lot_traceability", "route_exposure"],
        "metric_keys": ["shipment_units", "customs_hold_status", "component_lot_status", "transit_time_days", "supplier_site_exposure_share"],
        "required_fields": ["component_or_lane", "event_or_status", "value_or_status", "period_or_as_of", "source_owner"],
    },
    {
        "key": "cybersecurity_operating_signal",
        "destination": "analyst_evidence_dataset_research_request_if_missing",
        "fact_classes": ["vulnerability", "incident", "exploit", "risk_metric"],
        "metric_keys": ["vulnerability_count", "cloud_incident_capacity_impact", "outage_duration_hours"],
        "required_fields": ["vendor_or_scope", "severity_or_incident_type", "value_or_status", "period", "source_owner"],
    },
)


_METRICS_BY_KEY = {metric.key: metric for metric in MODEL_METRICS}
_METRIC_ALIAS_MAP: dict[str, str] = {}
for _metric in MODEL_METRICS:
    for _alias in (_metric.key, _metric.label, *_metric.aliases):
        normalized = re.sub(r"[^a-z0-9]+", "_", str(_alias).strip().lower()).strip("_")
        if normalized:
            _METRIC_ALIAS_MAP.setdefault(normalized, _metric.key)


INSTITUTIONAL_SOURCE_REGISTRY: tuple[dict[str, Any], ...] = (
    {"name": "Goldman Sachs", "actor_type": "firm", "side": "sell_side", "aliases": ["Goldman", "Goldman Sachs Research", "GS Research", "GS"]},
    {"name": "Morgan Stanley", "actor_type": "firm", "side": "sell_side", "aliases": ["Morgan Stanley Research", "MS Research", "MS"]},
    {"name": "J.P. Morgan", "actor_type": "firm", "side": "sell_side", "aliases": ["JP Morgan", "JPMorgan", "JPMorgan Chase", "JPM"]},
    {"name": "Bank of America", "actor_type": "firm", "side": "sell_side", "aliases": ["BofA", "BofA Securities", "Bank of America Securities"]},
    {"name": "UBS", "actor_type": "firm", "side": "sell_side", "aliases": ["UBS Research"]},
    {"name": "Jefferies", "actor_type": "firm", "side": "sell_side", "aliases": ["Jefferies Research"]},
    {"name": "Bernstein", "actor_type": "firm", "side": "sell_side", "aliases": ["Sanford C. Bernstein", "AllianceBernstein", "Bernstein Research"]},
    {"name": "Citi", "actor_type": "firm", "side": "sell_side", "aliases": ["Citigroup", "Citi Research"]},
    {"name": "Barclays", "actor_type": "firm", "side": "sell_side", "aliases": ["Barclays Research"]},
    {"name": "Deutsche Bank", "actor_type": "firm", "side": "sell_side", "aliases": ["Deutsche Bank Research"]},
    {"name": "HSBC", "actor_type": "firm", "side": "sell_side", "aliases": ["HSBC Research"]},
    {"name": "Nomura", "actor_type": "firm", "side": "sell_side", "aliases": ["Nomura Research"]},
    {"name": "Mizuho", "actor_type": "firm", "side": "sell_side", "aliases": ["Mizuho Securities", "Mizuho Research"]},
    {"name": "Daiwa", "actor_type": "firm", "side": "sell_side", "aliases": ["Daiwa Securities", "Daiwa Research"]},
    {"name": "Macquarie", "actor_type": "firm", "side": "sell_side", "aliases": ["Macquarie Research"]},
    {"name": "TD Cowen", "actor_type": "firm", "side": "sell_side", "aliases": ["Cowen", "Cowen Research"]},
    {"name": "Susquehanna", "actor_type": "firm", "side": "sell_side", "aliases": ["Susquehanna International Group", "SIG"]},
    {"name": "KeyBanc", "actor_type": "firm", "side": "sell_side", "aliases": ["KeyBanc Capital Markets"]},
    {"name": "Wolfe Research", "actor_type": "firm", "side": "sell_side", "aliases": ["Wolfe"]},
    {"name": "Redburn Atlantic", "actor_type": "firm", "side": "sell_side", "aliases": ["Redburn"]},
    {"name": "SemiAnalysis", "actor_type": "research_org", "side": "specialist_research", "handle": "SemiAnalysis_", "aliases": ["Semi Analysis"]},
    {"name": "Citrini Research", "actor_type": "research_org", "side": "specialist_research", "aliases": ["Citrini"]},
    {"name": "Irrational Analysis", "actor_type": "person", "side": "specialist_research", "aliases": ["IrrationalAnalysis"]},
    {"name": "PhotonCap", "actor_type": "person", "side": "specialist_research", "handle": "PhotonCap", "aliases": ["Photon Capital", "PhotonCap Research"]},
    {"name": "Collyer Bridge", "actor_type": "research_org", "side": "specialist_research", "aliases": ["Collyer Bridge Research"]},
    {"name": "TechInsights", "actor_type": "data_vendor", "side": "specialist_research", "aliases": ["Tech Insights"]},
    {"name": "TrendForce", "actor_type": "data_vendor", "side": "specialist_research", "aliases": ["DRAMeXchange"]},
    {"name": "Counterpoint Research", "actor_type": "data_vendor", "side": "specialist_research", "aliases": ["Counterpoint"]},
    {"name": "Omdia", "actor_type": "data_vendor", "side": "specialist_research", "aliases": ["IHS Markit Technology", "IHS Markit"]},
    {"name": "IDC", "actor_type": "data_vendor", "side": "specialist_research", "aliases": ["International Data Corporation"]},
    {"name": "Gartner", "actor_type": "data_vendor", "side": "specialist_research", "aliases": ["Gartner Research"]},
    {"name": "Yole Group", "actor_type": "data_vendor", "side": "specialist_research", "aliases": ["Yole", "Yole Developpement", "Yole Intelligence"]},
)


def _metric_alias_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def normalize_metric_key(value: object) -> str | None:
    key = _metric_alias_key(value)
    if not key:
        return None
    return _METRIC_ALIAS_MAP.get(key, key[:128])


def metric_definition(key: object) -> MetricDefinition | None:
    normalized = normalize_metric_key(key)
    return _METRICS_BY_KEY.get(normalized or "")


def metric_value_type_for_key(key: object) -> str | None:
    definition = metric_definition(key)
    return definition.value_type if definition is not None else None


def metric_state_field(key: object) -> str | None:
    definition = metric_definition(key)
    return definition.state_vector_field if definition is not None else None


def compact_metric_vocabulary(*, include_aliases: bool = True, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    metrics = MODEL_METRICS[: max(0, int(limit))] if limit is not None else MODEL_METRICS
    for metric in metrics:
        row: dict[str, Any] = {
            "key": metric.key,
            "value_type": metric.value_type,
            "period_required": metric.period_required,
        }
        if metric.state_vector_field:
            row["state_vector_field"] = metric.state_vector_field
        if metric.min_value is not None:
            row["min"] = metric.min_value
        if metric.max_value is not None:
            row["max"] = metric.max_value
        if metric.unit_examples:
            row["units"] = list(metric.unit_examples[:4])
        if include_aliases and metric.aliases:
            row["aliases"] = list(metric.aliases[:6])
        rows.append(row)
    return rows


def compact_signal_rail_taxonomy(*, limit: int | None = None) -> list[dict[str, Any]]:
    rails = MODEL_SIGNAL_RAILS[: max(0, int(limit))] if limit is not None else MODEL_SIGNAL_RAILS
    out: list[dict[str, Any]] = []
    for rail in rails:
        out.append(
            {
                "key": rail.get("key"),
                "destination": rail.get("destination"),
                "fact_classes": list(rail.get("fact_classes") or [])[:8],
                "metric_keys": list(rail.get("metric_keys") or []),
                "required_fields": list(rail.get("required_fields") or [])[:8],
            }
        )
    return out


def source_actor_alias_specs(item: dict[str, Any]) -> list[dict[str, str | None]]:
    specs: list[dict[str, str | None]] = [{"alias": str(item.get("name") or ""), "alias_kind": "name", "platform": None}]
    for alias in item.get("aliases") or []:
        if str(alias or "").strip():
            specs.append({"alias": str(alias), "alias_kind": "name", "platform": None})
            if normalize_actor_type(item.get("actor_type")) in {"firm", "research_org", "data_vendor"}:
                specs.append({"alias": str(alias), "alias_kind": "firm_name", "platform": None})
    if item.get("handle"):
        specs.append({"alias": str(item.get("handle")), "alias_kind": "x_handle", "platform": "x"})
    if normalize_actor_type(item.get("actor_type")) in {"firm", "research_org", "data_vendor"}:
        specs.append({"alias": str(item.get("name") or ""), "alias_kind": "firm_name", "platform": None})
    seen: set[tuple[str, str, str | None]] = set()
    out: list[dict[str, str | None]] = []
    for spec in specs:
        alias = str(spec.get("alias") or "").strip()
        if not alias:
            continue
        alias_kind = str(spec.get("alias_kind") or "name")
        platform = spec.get("platform")
        key = (normalize_source_alias(alias, alias_kind=alias_kind), alias_kind, platform)
        if key in seen:
            continue
        seen.add(key)
        out.append({"alias": alias, "alias_kind": alias_kind, "platform": platform})
    return out


def _find_actor_for_specs(session: Session, specs: list[dict[str, str | None]]) -> SourceActor | None:
    for spec in specs:
        found = find_source_actor_by_alias(
            session,
            alias=str(spec["alias"]),
            alias_kind=str(spec.get("alias_kind") or "name"),
            platform=spec.get("platform"),
        )
        if found is not None:
            return found
    return None


def ensure_registered_source_actor(
    session: Session,
    item: dict[str, Any],
    *,
    actor: str = "seed",
) -> SourceActor:
    name = str(item.get("name") or "").strip()
    if not name:
        raise ValueError("source registry item requires name")
    specs = source_actor_alias_specs(item)
    row = _find_actor_for_specs(session, specs)
    payload = {
        "source_registry": SOURCE_REGISTRY_VERSION,
        "source_registry_role": "institutional_source",
        "registry_aliases": [spec["alias"] for spec in specs[:12]],
    }
    payload.update(dict(item.get("payload") or {}))
    actor_type = normalize_actor_type(item.get("actor_type"))
    side = normalize_side(item.get("side")) if item.get("side") else None
    if row is None:
        row = ensure_source_actor(
            session,
            display_name=name,
            actor_type=actor_type,
            side=side,
            trust_label=str(item.get("trust_label") or "").strip()[:64] or None,
            notes=str(item.get("notes") or "").strip() or None,
            payload=payload,
            actor=actor,
        )
    else:
        now = utcnow()
        if actor_type != "unknown" and row.actor_type in {"unknown", "account"}:
            row.actor_type = actor_type
        if side and not row.side:
            row.side = side
        if item.get("trust_label") and not row.trust_label:
            row.trust_label = str(item.get("trust_label")).strip()[:64]
        if item.get("notes") and not row.notes:
            row.notes = str(item.get("notes")).strip()
        merged = dict(row.payload or {})
        merged.update(payload)
        row.payload = merged
        row.updated_at = now
        session.add(row)
        session.flush()
    for spec in specs:
        ensure_source_actor_alias(
            session,
            row,
            alias=str(spec["alias"]),
            alias_kind=str(spec.get("alias_kind") or "name"),
            platform=spec.get("platform"),
            confidence=1.0,
            payload={"source_registry": SOURCE_REGISTRY_VERSION},
        )
    return row


def seed_institutional_source_registry(session: Session) -> dict[str, int]:
    actors_before = session.query(SourceActor).count()
    aliases_before = session.query(SourceActorAlias).count()
    for item in INSTITUTIONAL_SOURCE_REGISTRY:
        ensure_registered_source_actor(session, item, actor="seed")
    session.flush()
    return {
        "registry_items": len(INSTITUTIONAL_SOURCE_REGISTRY),
        "actors_created": max(0, session.query(SourceActor).count() - actors_before),
        "aliases_created": max(0, session.query(SourceActorAlias).count() - aliases_before),
    }


def serialize_source_actor_for_agent(actor: SourceActor, *, alias_limit: int = 8) -> dict[str, Any]:
    aliases = [
        {
            "alias": row.alias,
            "alias_kind": row.alias_kind,
            "platform": row.platform,
        }
        for row in sorted(list(actor.aliases or []), key=lambda item: (str(item.alias_kind), str(item.alias)))[:alias_limit]
    ]
    return {
        "id": int(actor.id),
        "actor_key": actor.actor_key,
        "display_name": actor.display_name,
        "actor_type": actor.actor_type,
        "side": actor.side,
        "trust_label": actor.trust_label,
        "aliases": aliases,
    }
