from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime
from io import BytesIO, StringIO
from threading import RLock
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.dataset_catalog import (
    DATASET_FAMILY_COLUMN_TEMPLATES,
    DATASET_FAMILY_MATERIALIZER_FAMILIES,
    DATASET_FAMILY_META,
    DATASET_FAMILY_REQUIRED_ROLES,
    DATASET_OBJECT_TYPES,
    DATASET_VIEW_OPERATOR_VALUES,
    DATASET_VIEW_OPERATORS,
)
from thoth_terminal.models import (
    ChartDatasetView,
    Dataset,
    DatasetColumn,
    Document,
    DatasetLink,
    DatasetRow,
    DatasetVersion,
    DatasetView,
    InstrumentRequest,
    ProductStageEntry,
    StructuredDataFact,
)
from thoth_terminal.product_stage_service import serialize_product_stage_entry


_FAMILY_MATERIALIZER_UNKNOWN_DEFAULTS: dict[str, tuple[str, ...]] = {
    "trade_customs": (
        "reporter",
        "reporter_iso",
        "partner_scope",
        "flow",
        "hs_code",
        "metric",
    ),
    "monthly_revenue": (
        "company",
        "ticker",
        "exchange",
        "source_authority",
        "metric",
    ),
    "analyst_evidence": (
        "company",
        "ticker",
        "broker",
        "analyst",
        "rating_action",
        "current_rating",
    ),
    "corporate_action_event": (
        "acquirer",
        "target",
        "deal_type",
        "deal_status",
    ),
    "government_contract_award": (
        "agency",
        "prime_contractor",
        "program",
        "product_or_service",
        "award_id",
    ),
    "clinical_regulatory_milestone": (
        "asset",
        "company",
        "indication",
        "phase",
        "event_type",
    ),
    "healthcare_regulatory_source_ingestion": (
        "source_family",
        "authority",
        "record_id",
        "record_status",
        "backfill_status",
    ),
    "pharma_commercial_supply_access": (
        "asset",
        "company",
        "indication",
        "geography",
        "channel",
        "shortage_status",
    ),
    "policy_incentive_trade_cost": (
        "program_or_law",
        "jurisdiction",
        "authority",
        "beneficiary",
        "incentive_type",
        "application_status",
    ),
    "macro_market_context": (
        "geography",
        "source_role",
        "estimate_owner",
        "scenario",
        "metric",
    ),
    "fx_currency_hedging_impact": (
        "company",
        "ticker",
        "segment",
        "geography",
        "currency_pair",
        "exposure_type",
        "exposure_basis",
        "metric",
    ),
    "eo_geoint_data_service_economics": (
        "provider",
        "customer_or_agency",
        "mission_or_use_case",
        "sensor_type",
        "modality",
    ),
    "spacecraft_bus_payload_integration": (
        "prime_contractor",
        "program_or_constellation",
        "bus_class",
        "payload_type",
        "constraint_subsystem",
    ),
    "orbital_edge_compute_service": (
        "provider",
        "customer",
        "mission_or_service_type",
        "orbit",
        "station_or_platform",
    ),
    "humanoid_robot_bom_manufacturing": (
        "robot_oem",
        "robot_platform",
        "supplier",
        "component_category",
        "customer_or_program",
    ),
    "robot_fleet_operations": (
        "robot_oem",
        "robot_platform",
        "customer",
        "site_or_facility",
        "use_case",
    ),
    "robot_deployment_economics": (
        "provider",
        "robot_platform",
        "customer",
        "workflow_owner",
        "use_case",
    ),
    "robot_perception_manipulation": (
        "supplier",
        "robot_platform",
        "component_family",
        "component_type",
    ),
    "industrial_automation_demand": (
        "supplier",
        "customer",
        "end_market",
        "application",
        "product_family",
    ),
    "saas_software_operating_kpi": (
        "company",
        "segment",
        "product",
        "geography",
        "metric",
    ),
    "consumer_subscription_engagement": (
        "company",
        "platform",
        "product",
        "region",
        "metric",
    ),
    "advertising_retail_media_adtech": (
        "platform",
        "channel",
        "format",
        "region",
        "metric",
    ),
    "media_ip_content_rights": (
        "title",
        "studio_or_platform",
        "release_window",
        "metric",
    ),
    "marketplace_payment_network_metrics": (
        "platform",
        "product",
        "region",
        "metric",
    ),
    "platform_distribution_economics": (
        "platform",
        "partner",
        "payer",
        "recipient",
        "product_category",
        "metric",
    ),
    "tower_infrastructure_lease_economics": (
        "tower_owner",
        "carrier_customer",
        "region",
        "metric",
    ),
    "data_center_leasing_power_procurement": (
        "operator_or_landlord",
        "tenant_customer_offtaker",
        "seller_project_sponsor",
        "site_project",
        "market_node_utility",
        "metric",
    ),
    "financial_insurance_private_credit_kpi": (
        "company",
        "segment",
        "portfolio_type",
        "geography",
        "metric",
    ),
    "exchange_clearing_market_data": (
        "exchange",
        "asset_class",
        "product_contract",
        "region",
        "metric",
    ),
    "commodity_producer_margin": (
        "company",
        "commodity",
        "product_grade",
        "benchmark",
        "region",
        "metric",
    ),
    "workforce_staffing_kpi": (
        "company",
        "segment",
        "geography",
        "metric",
    ),
    "freight_logistics_indicator": (
        "mode",
        "carrier_operator",
        "route",
        "origin",
        "destination",
        "metric",
    ),
    "channel_distribution_inventory": (
        "company",
        "product",
        "channel",
        "region",
        "distributor",
        "metric",
    ),
    "retail_restaurant_franchise_kpi": (
        "company",
        "brand",
        "region",
        "ownership_model",
        "metric",
    ),
    "healthcare_utilization_lifescience_tools": (
        "company",
        "segment",
        "product_system",
        "indication_procedure",
        "region",
        "metric",
    ),
    "managed_care_payer_pbm_kpi": (
        "company",
        "segment",
        "product",
        "geography",
        "metric",
    ),
    "auto_oem_operating_kpi": (
        "oem",
        "model",
        "region",
        "channel",
        "metric",
    ),
    "cpg_category_price_mix": (
        "company",
        "brand",
        "product",
        "category",
        "channel",
        "region",
        "metric",
    ),
    "industrial_aftermarket_installed_base": (
        "manufacturer",
        "equipment_category",
        "end_market",
        "region",
        "metric",
    ),
    "product_quality_warranty_rma": (
        "company",
        "product_model",
        "component_subsystem",
        "defect_failure_mode",
        "metric",
    ),
    "environmental_services_waste_kpi": (
        "company",
        "service_line",
        "region",
        "metric",
    ),
    "property_reit_operating_metric": (
        "owner",
        "sector",
        "property",
        "market",
        "metric",
    ),
    "source_consensus_conflict": (
        "subject_type",
        "entity",
        "metric",
        "source_owner",
        "conflict_type",
        "conflict_state",
    ),
    "energy_permit_source_ingestion": (
        "source_family",
        "authority",
        "source_system",
        "record_kind",
        "coverage_geography",
        "destination_family",
    ),
    "export_control_market_access": (
        "jurisdiction",
        "authority",
        "rule_name",
        "restriction_type",
        "affected_product",
        "license_status",
    ),
    "china_semiconductor_localization": (
        "company",
        "supplier",
        "product",
        "node_or_process",
        "import_substitution_status",
        "qualification_status",
    ),
    "capacity_contract_financing": (
        "payer_customer",
        "supplier",
        "asset_or_capacity_type",
        "product_or_platform",
        "facility_or_program",
        "payment_type",
        "contract_status",
    ),
    "passive_component_supply": (
        "supplier",
        "component_family",
        "component_type",
        "technology_family",
        "application",
        "platform_or_program",
    ),
    "pcb_laminate_supply": (
        "supplier",
        "product_category",
        "pcb_type",
        "ccl_type",
        "copper_foil_type",
        "laminate_grade",
        "application",
        "platform_or_program",
    ),
    "ai_networking_fabric": (
        "supplier",
        "component_family",
        "component_type",
        "fabric_standard",
        "fabric_domain",
        "topology",
        "platform_or_program",
    ),
    "ai_board_power_delivery": (
        "supplier",
        "power_device_family",
        "component_type",
        "power_tree_topology",
        "platform_or_program",
    ),
    "telecom_network_deployment": (
        "carrier_operator",
        "vendor",
        "region",
        "route_or_metro",
        "equipment_scope",
    ),
    "telecom_service_demand": (
        "carrier",
        "region",
        "service_type",
        "customer_segment",
    ),
    "facility_capex_ramp": (
        "company",
        "facility",
        "project",
        "site_or_asset",
        "facility_type",
        "facility_status",
        "product_or_resource",
        "technology_or_process",
        "customer_or_program",
        "stage",
        "source_basis",
        "timing_basis",
        "capacity_basis",
        "metric",
    ),
}


def _family_materializer_apply_defaults(family: str, values: dict[str, Any]) -> dict[str, Any]:
    for key in _FAMILY_MATERIALIZER_UNKNOWN_DEFAULTS.get(family, ()):
        if key in values and values.get(key) in (None, ""):
            values[key] = "unknown"
    return values


def _family_materializer_can_upgrade_schema(existing_keys: list[str], columns: list[dict[str, str]]) -> bool:
    template_keys = {str(column.get("key") or "") for column in columns}
    alias_keys = {
        alias
        for target_key, aliases in _FAMILY_MATERIALIZER_VALUE_ALIASES.items()
        if target_key in template_keys
        for alias in aliases
    }
    return all(key in template_keys or key in alias_keys for key in existing_keys)
