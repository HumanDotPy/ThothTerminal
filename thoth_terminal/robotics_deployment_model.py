from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any


DEFAULT_ANNUAL_SCHEDULABLE_HOURS = 8760.0
DEFAULT_UTILIZATION = 0.5
DEFAULT_PRODUCTIVITY_VS_HUMAN = 1.0
DEFAULT_USEFUL_LIFE_YEARS = 2.0
DEFAULT_LOADED_LABOR_COST = 30.0
DEFAULT_INTERVENTION_LABOR_COST = 30.0


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(out):
        return None
    return out


def _bounded_rate(value: object, *, default: float | None = None) -> float | None:
    out = _float_or_none(value)
    if out is None:
        return default
    if out > 1.0 and out <= 100.0:
        out = out / 100.0
    return min(max(out, 0.0), 1.0)


def _positive(value: object, *, default: float | None = None) -> float | None:
    out = _float_or_none(value)
    if out is None:
        return default
    return out if out > 0 else None


def _non_negative(value: object, *, default: float = 0.0) -> float:
    out = _float_or_none(value)
    if out is None:
        return default
    return max(out, 0.0)


def _string(value: object, *, default: str = "") -> str:
    out = str(value or "").strip()
    return out or default


@dataclass
class AssumptionAudit:
    missing_required: list[str] = field(default_factory=list)
    defaults_used: dict[str, float | str] = field(default_factory=dict)
    estimated_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def completeness(self) -> str:
        if self.missing_required:
            return "insufficient"
        if self.defaults_used or self.estimated_fields:
            return "partial"
        return "complete"

    def to_dict(self) -> dict[str, object]:
        out = asdict(self)
        out["completeness"] = self.completeness
        return out


@dataclass
class RegionAssumptions:
    region: str = "unspecified"
    currency: str = "USD"
    loaded_labor_cost_per_hour: float = DEFAULT_LOADED_LABOR_COST
    wage_per_hour: float | None = None
    payroll_tax_rate: float | None = None
    benefits_uplift_rate: float | None = None
    other_labor_uplift_rate: float | None = None
    loaded_labor_cost_source: str = "default"
    tariff_rate: float = 0.0
    shipping_cost_per_robot: float = 0.0
    localization_cost_per_robot: float = 0.0
    energy_cost_per_kwh: float = 0.0
    service_labor_cost_per_hour: float = DEFAULT_INTERVENTION_LABOR_COST


@dataclass
class OperatingCycleAssumptions:
    annual_schedulable_hours: float = DEFAULT_ANNUAL_SCHEDULABLE_HOURS
    utilization: float = DEFAULT_UTILIZATION
    technical_availability: float = 1.0
    battery_life_hours: float | None = None
    charge_time_hours: float | None = None
    hot_swappable_battery: bool = False
    battery_swap_time_minutes: float = 0.0


@dataclass
class RobotCostAssumptions:
    asp: float | None = None
    useful_life_years: float = DEFAULT_USEFUL_LIFE_YEARS
    annual_maintenance_cost: float = 0.0
    annual_software_cost: float = 0.0
    annual_insurance_cost: float = 0.0
    annual_field_service_cost: float = 0.0
    annual_repair_cost: float = 0.0
    repair_downtime_hours_per_year: float = 0.0
    spare_unit_buffer_pct: float = 0.0
    deployment_setup_cost: float = 0.0
    energy_kwh_per_robot_hour: float = 0.0
    residual_value: float = 0.0


@dataclass
class InterventionAssumptions:
    minutes_per_robot_hour: float = 0.0
    labor_cost_per_hour: float = DEFAULT_INTERVENTION_LABOR_COST


@dataclass
class DeploymentAssumptions:
    deployment_mode: str = "internal"
    robot_oem: str = ""
    robot_platform: str = ""
    domain: str = ""
    task: str = ""
    productivity_vs_human: float = DEFAULT_PRODUCTIVITY_VS_HUMAN
    task_demand_coverage: float = 1.0
    task_share_of_role: float | None = None
    customer_upfront_cost: float | None = None
    customer_hourly_price: float | None = None
    required_customer_savings_share: float | None = None


@dataclass
class RoboticsDeploymentScenario:
    region: RegionAssumptions
    operating_cycle: OperatingCycleAssumptions
    robot_cost: RobotCostAssumptions
    intervention: InterventionAssumptions
    deployment: DeploymentAssumptions
    audit: AssumptionAudit


def robotics_scenario_from_payload(payload: dict[str, object]) -> RoboticsDeploymentScenario:
    audit = AssumptionAudit()

    def assumed(name: str, default: float | str) -> None:
        audit.defaults_used[name] = default

    region_payload = payload.get("region")
    region_data = region_payload if isinstance(region_payload, dict) else {}
    direct_loaded_labor = _positive(payload.get("loaded_labor_cost_per_hour", region_data.get("loaded_labor_cost_per_hour")))
    wage_per_hour = _positive(
        payload.get("wage_per_hour", payload.get("base_wage_per_hour", region_data.get("wage_per_hour", region_data.get("base_wage_per_hour"))))
    )
    payroll_tax_rate = _bounded_rate(
        payload.get("payroll_tax_rate", payload.get("employer_payroll_tax_rate", region_data.get("payroll_tax_rate", region_data.get("employer_payroll_tax_rate"))))
    )
    benefits_uplift_rate = _bounded_rate(
        payload.get("benefits_uplift_rate", payload.get("benefits_rate", region_data.get("benefits_uplift_rate", region_data.get("benefits_rate"))))
    )
    other_labor_uplift_rate = _bounded_rate(
        payload.get("other_labor_uplift_rate", region_data.get("other_labor_uplift_rate"))
    )
    loaded_labor_source = "direct"
    if direct_loaded_labor is not None:
        loaded_labor = direct_loaded_labor
    elif wage_per_hour is not None:
        loaded_labor_source = "derived_from_wage"
        loaded_labor = wage_per_hour * (
            1.0
            + float(payroll_tax_rate or 0.0)
            + float(benefits_uplift_rate or 0.0)
            + float(other_labor_uplift_rate or 0.0)
        )
        audit.estimated_fields.append("loaded_labor_cost_per_hour")
    else:
        loaded_labor_source = "default"
        loaded_labor = DEFAULT_LOADED_LABOR_COST
        assumed("loaded_labor_cost_per_hour", DEFAULT_LOADED_LABOR_COST)
    service_labor = _positive(
        payload.get("service_labor_cost_per_hour", region_data.get("service_labor_cost_per_hour")),
        default=loaded_labor,
    )
    region = RegionAssumptions(
        region=_string(payload.get("region_name", region_data.get("region")), default="unspecified"),
        currency=_string(payload.get("currency", region_data.get("currency")), default="USD"),
        loaded_labor_cost_per_hour=float(loaded_labor or DEFAULT_LOADED_LABOR_COST),
        wage_per_hour=wage_per_hour,
        payroll_tax_rate=payroll_tax_rate,
        benefits_uplift_rate=benefits_uplift_rate,
        other_labor_uplift_rate=other_labor_uplift_rate,
        loaded_labor_cost_source=loaded_labor_source,
        tariff_rate=float(_bounded_rate(payload.get("tariff_rate", region_data.get("tariff_rate")), default=0.0) or 0.0),
        shipping_cost_per_robot=_non_negative(payload.get("shipping_cost_per_robot", region_data.get("shipping_cost_per_robot"))),
        localization_cost_per_robot=_non_negative(payload.get("localization_cost_per_robot", region_data.get("localization_cost_per_robot"))),
        energy_cost_per_kwh=_non_negative(payload.get("energy_cost_per_kwh", region_data.get("energy_cost_per_kwh"))),
        service_labor_cost_per_hour=float(service_labor or DEFAULT_INTERVENTION_LABOR_COST),
    )

    operating_payload = payload.get("operating_cycle")
    operating_data = operating_payload if isinstance(operating_payload, dict) else {}
    utilization = _bounded_rate(payload.get("utilization", operating_data.get("utilization")), default=DEFAULT_UTILIZATION)
    if "utilization" not in payload and "utilization" not in operating_data:
        assumed("utilization", DEFAULT_UTILIZATION)
    useful_life = _positive(payload.get("useful_life_years"), default=DEFAULT_USEFUL_LIFE_YEARS)
    if "useful_life_years" not in payload:
        assumed("useful_life_years", DEFAULT_USEFUL_LIFE_YEARS)
    operating_cycle = OperatingCycleAssumptions(
        annual_schedulable_hours=float(
            _positive(payload.get("annual_schedulable_hours", operating_data.get("annual_schedulable_hours")), default=DEFAULT_ANNUAL_SCHEDULABLE_HOURS)
            or DEFAULT_ANNUAL_SCHEDULABLE_HOURS
        ),
        utilization=float(utilization or DEFAULT_UTILIZATION),
        technical_availability=float(_bounded_rate(payload.get("technical_availability", operating_data.get("technical_availability")), default=1.0) or 1.0),
        battery_life_hours=_positive(payload.get("battery_life_hours", operating_data.get("battery_life_hours"))),
        charge_time_hours=_positive(payload.get("charge_time_hours", operating_data.get("charge_time_hours"))),
        hot_swappable_battery=bool(payload.get("hot_swappable_battery", operating_data.get("hot_swappable_battery", False))),
        battery_swap_time_minutes=_non_negative(payload.get("battery_swap_time_minutes", operating_data.get("battery_swap_time_minutes"))),
    )

    asp = _positive(payload.get("asp", payload.get("robot_price")))
    if asp is None:
        audit.missing_required.append("asp")
    robot_cost = RobotCostAssumptions(
        asp=asp,
        useful_life_years=float(useful_life or DEFAULT_USEFUL_LIFE_YEARS),
        annual_maintenance_cost=_non_negative(payload.get("annual_maintenance_cost")),
        annual_software_cost=_non_negative(payload.get("annual_software_cost")),
        annual_insurance_cost=_non_negative(payload.get("annual_insurance_cost")),
        annual_field_service_cost=_non_negative(payload.get("annual_field_service_cost")),
        annual_repair_cost=_non_negative(payload.get("annual_repair_cost")),
        repair_downtime_hours_per_year=_non_negative(payload.get("repair_downtime_hours_per_year")),
        spare_unit_buffer_pct=float(_bounded_rate(payload.get("spare_unit_buffer_pct"), default=0.0) or 0.0),
        deployment_setup_cost=_non_negative(payload.get("deployment_setup_cost")),
        energy_kwh_per_robot_hour=_non_negative(payload.get("energy_kwh_per_robot_hour")),
        residual_value=_non_negative(payload.get("residual_value")),
    )
    estimated_fields = payload.get("estimated_fields")
    if isinstance(estimated_fields, list):
        audit.estimated_fields = [str(item) for item in estimated_fields if str(item).strip()]

    intervention_minutes = _non_negative(payload.get("intervention_minutes_per_robot_hour", payload.get("human_intervention_minutes_per_hour")))
    if "intervention_minutes_per_robot_hour" not in payload and "human_intervention_minutes_per_hour" not in payload:
        assumed("intervention_minutes_per_robot_hour", 0.0)
    intervention = InterventionAssumptions(
        minutes_per_robot_hour=intervention_minutes,
        labor_cost_per_hour=float(_positive(payload.get("intervention_labor_cost_per_hour"), default=region.service_labor_cost_per_hour) or region.service_labor_cost_per_hour),
    )

    productivity = _positive(payload.get("productivity_vs_human"), default=DEFAULT_PRODUCTIVITY_VS_HUMAN)
    if "productivity_vs_human" not in payload:
        assumed("productivity_vs_human", DEFAULT_PRODUCTIVITY_VS_HUMAN)
    task_demand_coverage = _bounded_rate(payload.get("task_demand_coverage"), default=1.0)
    deployment = DeploymentAssumptions(
        deployment_mode=_string(payload.get("deployment_mode"), default="internal"),
        robot_oem=_string(payload.get("robot_oem")),
        robot_platform=_string(payload.get("robot_platform")),
        domain=_string(payload.get("domain")),
        task=_string(payload.get("task")),
        productivity_vs_human=float(productivity or DEFAULT_PRODUCTIVITY_VS_HUMAN),
        task_demand_coverage=float(task_demand_coverage or 1.0),
        task_share_of_role=_bounded_rate(payload.get("task_share_of_role")),
        customer_upfront_cost=_positive(payload.get("customer_upfront_cost")),
        customer_hourly_price=_positive(payload.get("customer_hourly_price")),
        required_customer_savings_share=_bounded_rate(payload.get("required_customer_savings_share")),
    )

    if deployment.productivity_vs_human <= 0:
        audit.missing_required.append("productivity_vs_human")
    if region.loaded_labor_cost_per_hour <= 0:
        audit.missing_required.append("loaded_labor_cost_per_hour")

    return RoboticsDeploymentScenario(
        region=region,
        operating_cycle=operating_cycle,
        robot_cost=robot_cost,
        intervention=intervention,
        deployment=deployment,
        audit=audit,
    )


def _battery_cycle_availability(cycle: OperatingCycleAssumptions, audit: AssumptionAudit) -> float:
    if not cycle.battery_life_hours:
        return 1.0
    downtime = 0.0
    if cycle.hot_swappable_battery:
        downtime = cycle.battery_swap_time_minutes / 60.0
    elif cycle.charge_time_hours:
        downtime = cycle.charge_time_hours
    else:
        audit.warnings.append("battery_life_hours supplied without charge_time_hours or hot-swap downtime; cycle availability assumed at 100%.")
        return 1.0
    denominator = cycle.battery_life_hours + downtime
    if denominator <= 0:
        return 1.0
    return min(max(cycle.battery_life_hours / denominator, 0.0), 1.0)


def _regional_robot_price(scenario: RoboticsDeploymentScenario) -> float | None:
    asp = scenario.robot_cost.asp
    if asp is None:
        return None
    return (
        asp * (1.0 + scenario.region.tariff_rate)
        + scenario.region.shipping_cost_per_robot
        + scenario.region.localization_cost_per_robot
    )


def evaluate_robotics_deployment(payload: dict[str, object]) -> dict[str, object]:
    scenario = robotics_scenario_from_payload(payload)
    audit = scenario.audit
    cycle_availability = _battery_cycle_availability(scenario.operating_cycle, audit)
    annual_productive_robot_hours = (
        scenario.operating_cycle.annual_schedulable_hours
        * scenario.operating_cycle.utilization
        * scenario.operating_cycle.technical_availability
        * cycle_availability
    )
    repair_downtime_hours = min(
        scenario.robot_cost.repair_downtime_hours_per_year,
        annual_productive_robot_hours,
    )
    repair_buffer_hours = repair_downtime_hours * scenario.robot_cost.spare_unit_buffer_pct
    annual_productive_robot_hours = max(
        annual_productive_robot_hours - repair_downtime_hours + repair_buffer_hours,
        0.0,
    )
    addressable_task_hours_per_year = (
        scenario.operating_cycle.annual_schedulable_hours
        * scenario.deployment.task_demand_coverage
    )
    demand_limited_robot_hours = annual_productive_robot_hours
    if scenario.deployment.productivity_vs_human > 0:
        demand_limited_robot_hours = min(
            annual_productive_robot_hours,
            addressable_task_hours_per_year / scenario.deployment.productivity_vs_human,
        )
    if annual_productive_robot_hours <= 0:
        audit.missing_required.append("annual_productive_robot_hours")

    regional_price = _regional_robot_price(scenario)
    recurring_annual_cost = (
        scenario.robot_cost.annual_maintenance_cost
        + scenario.robot_cost.annual_software_cost
        + scenario.robot_cost.annual_insurance_cost
        + scenario.robot_cost.annual_field_service_cost
        + scenario.robot_cost.annual_repair_cost
    )
    recurring_cost_per_robot_hour = recurring_annual_cost / demand_limited_robot_hours if demand_limited_robot_hours > 0 else None
    energy_cost_per_robot_hour = scenario.robot_cost.energy_kwh_per_robot_hour * scenario.region.energy_cost_per_kwh
    intervention_cost_per_robot_hour = (
        scenario.intervention.minutes_per_robot_hour / 60.0 * scenario.intervention.labor_cost_per_hour
    )
    useful_life_productive_hours = demand_limited_robot_hours * scenario.robot_cost.useful_life_years
    deployment_setup_per_hour = scenario.robot_cost.deployment_setup_cost / useful_life_productive_hours if useful_life_productive_hours > 0 else None

    depreciation_per_robot_hour: float | None = None
    if regional_price is not None and useful_life_productive_hours > 0:
        depreciable_cost = max(regional_price - scenario.robot_cost.residual_value, 0.0)
        depreciation_per_robot_hour = depreciable_cost / useful_life_productive_hours

    recurring_hourly = None
    if recurring_cost_per_robot_hour is not None and deployment_setup_per_hour is not None:
        recurring_hourly = recurring_cost_per_robot_hour + deployment_setup_per_hour + energy_cost_per_robot_hour + intervention_cost_per_robot_hour
    robot_cost_per_robot_hour = None
    effective_cost_per_human_equivalent_hour = None
    if depreciation_per_robot_hour is not None and recurring_hourly is not None:
        robot_cost_per_robot_hour = depreciation_per_robot_hour + recurring_hourly
        effective_cost_per_human_equivalent_hour = robot_cost_per_robot_hour / scenario.deployment.productivity_vs_human

    human_equivalent_hours_per_year = demand_limited_robot_hours * scenario.deployment.productivity_vs_human
    annual_labor_value = human_equivalent_hours_per_year * scenario.region.loaded_labor_cost_per_hour
    annual_robot_cash_opex = demand_limited_robot_hours * (
        (recurring_cost_per_robot_hour or 0.0)
        + energy_cost_per_robot_hour
        + intervention_cost_per_robot_hour
    )
    customer_hourly_price = scenario.deployment.customer_hourly_price
    if customer_hourly_price is not None:
        annual_robot_cash_opex += demand_limited_robot_hours * customer_hourly_price
    annual_customer_cash_savings = annual_labor_value - annual_robot_cash_opex
    customer_upfront_cost = scenario.deployment.customer_upfront_cost
    if customer_upfront_cost is None:
        customer_upfront_cost = (regional_price or 0.0) + scenario.robot_cost.deployment_setup_cost
    payback_months = None
    if customer_upfront_cost > 0 and annual_customer_cash_savings > 0:
        payback_months = customer_upfront_cost / annual_customer_cash_savings * 12.0

    deployable = None
    cost_advantage_per_human_equivalent_hour = None
    if effective_cost_per_human_equivalent_hour is not None:
        cost_advantage_per_human_equivalent_hour = (
            scenario.region.loaded_labor_cost_per_hour - effective_cost_per_human_equivalent_hour
        )
        deployable = effective_cost_per_human_equivalent_hour <= scenario.region.loaded_labor_cost_per_hour

    solve_for = _solve_for_values(
        scenario=scenario,
        annual_productive_robot_hours=demand_limited_robot_hours,
        variable_cost_per_robot_hour=energy_cost_per_robot_hour + intervention_cost_per_robot_hour,
        recurring_annual_cost=recurring_annual_cost,
        regional_price=regional_price,
    )

    return {
        "scenario": {
            "deployment_mode": scenario.deployment.deployment_mode,
            "robot_oem": scenario.deployment.robot_oem,
            "robot_platform": scenario.deployment.robot_platform,
            "domain": scenario.deployment.domain,
            "task": scenario.deployment.task,
            "region": scenario.region.region,
            "currency": scenario.region.currency,
        },
        "inputs_normalized": {
            "region": asdict(scenario.region),
            "operating_cycle": asdict(scenario.operating_cycle),
            "robot_cost": asdict(scenario.robot_cost),
            "intervention": asdict(scenario.intervention),
            "deployment": asdict(scenario.deployment),
        },
        "outputs": {
            "deployable_at_labor_parity": deployable,
            "regional_robot_price": regional_price,
            "annual_productive_robot_hours": annual_productive_robot_hours,
            "repair_downtime_hours_per_year": repair_downtime_hours,
            "repair_buffer_hours_per_year": repair_buffer_hours,
            "demand_limited_robot_hours": demand_limited_robot_hours,
            "addressable_task_hours_per_year": addressable_task_hours_per_year,
            "human_equivalent_hours_per_year": human_equivalent_hours_per_year,
            "cycle_availability": cycle_availability,
            "useful_life_productive_hours": useful_life_productive_hours,
            "depreciation_per_robot_hour": depreciation_per_robot_hour,
            "recurring_cost_per_robot_hour": recurring_cost_per_robot_hour,
            "deployment_setup_per_robot_hour": deployment_setup_per_hour,
            "energy_cost_per_robot_hour": energy_cost_per_robot_hour,
            "intervention_cost_per_robot_hour": intervention_cost_per_robot_hour,
            "robot_cost_per_robot_hour": robot_cost_per_robot_hour,
            "effective_cost_per_human_equivalent_hour": effective_cost_per_human_equivalent_hour,
            "regional_loaded_labor_cost_per_hour": scenario.region.loaded_labor_cost_per_hour,
            "cost_advantage_per_human_equivalent_hour": cost_advantage_per_human_equivalent_hour,
            "annual_labor_value": annual_labor_value,
            "annual_robot_cash_opex": annual_robot_cash_opex,
            "annual_customer_cash_savings": annual_customer_cash_savings,
            "customer_upfront_cost": customer_upfront_cost,
            "customer_payback_months": payback_months,
        },
        "solve_for": solve_for,
        "audit": audit.to_dict(),
    }


def _solve_for_values(
    *,
    scenario: RoboticsDeploymentScenario,
    annual_productive_robot_hours: float,
    variable_cost_per_robot_hour: float,
    recurring_annual_cost: float,
    regional_price: float | None,
) -> dict[str, object]:
    labor = scenario.region.loaded_labor_cost_per_hour
    productivity = scenario.deployment.productivity_vs_human
    useful_life_hours = annual_productive_robot_hours * scenario.robot_cost.useful_life_years
    tariff_multiplier = 1.0 + scenario.region.tariff_rate
    landed_fixed = scenario.region.shipping_cost_per_robot + scenario.region.localization_cost_per_robot
    fixed_annual_equivalent = recurring_annual_cost + (scenario.robot_cost.deployment_setup_cost / scenario.robot_cost.useful_life_years)
    recurring = variable_cost_per_robot_hour
    if annual_productive_robot_hours > 0:
        recurring += fixed_annual_equivalent / annual_productive_robot_hours
    max_regional_asp = None
    max_base_asp = None
    if useful_life_hours > 0:
        max_regional_asp = (
            (labor * productivity - variable_cost_per_robot_hour) * useful_life_hours
            - fixed_annual_equivalent * scenario.robot_cost.useful_life_years
            + scenario.robot_cost.residual_value
        )
        max_regional_asp = max(max_regional_asp, 0.0)
        if tariff_multiplier > 0:
            max_base_asp = max((max_regional_asp - landed_fixed) / tariff_multiplier, 0.0)

    required_productivity = None
    if regional_price is not None and useful_life_hours > 0 and labor > 0:
        depreciation = max(regional_price - scenario.robot_cost.residual_value, 0.0) / useful_life_hours
        required_productivity = (depreciation + recurring) / labor

    required_utilization = None
    hourly_margin_before_fixed = labor * productivity - variable_cost_per_robot_hour
    if regional_price is not None and hourly_margin_before_fixed > 0:
        fixed_annual_with_robot_price = (
            max(regional_price - scenario.robot_cost.residual_value, 0.0) / scenario.robot_cost.useful_life_years
            + fixed_annual_equivalent
        )
        required_hours = fixed_annual_with_robot_price / hourly_margin_before_fixed
        base_hours = scenario.operating_cycle.annual_schedulable_hours * scenario.operating_cycle.technical_availability
        base_hours *= _battery_cycle_availability(scenario.operating_cycle, scenario.audit)
        if base_hours > 0:
            required_utilization = required_hours / base_hours

    max_intervention_minutes_per_robot_hour = None
    non_intervention_variable_hourly = variable_cost_per_robot_hour - (
        scenario.intervention.minutes_per_robot_hour / 60.0 * scenario.intervention.labor_cost_per_hour
    )
    if scenario.intervention.labor_cost_per_hour > 0:
        max_intervention_cost = labor * productivity - non_intervention_variable_hourly
        if annual_productive_robot_hours > 0:
            max_intervention_cost -= fixed_annual_equivalent / annual_productive_robot_hours
        if regional_price is not None and useful_life_hours > 0:
            max_intervention_cost -= max(regional_price - scenario.robot_cost.residual_value, 0.0) / useful_life_hours
        max_intervention_minutes_per_robot_hour = max(max_intervention_cost / scenario.intervention.labor_cost_per_hour * 60.0, 0.0)

    return {
        "max_regional_asp_at_labor_parity": max_regional_asp,
        "max_base_asp_at_labor_parity": max_base_asp,
        "required_productivity_vs_human_at_labor_parity": required_productivity,
        "required_utilization_at_labor_parity": required_utilization,
        "max_intervention_minutes_per_robot_hour_at_labor_parity": max_intervention_minutes_per_robot_hour,
    }


def robotics_deployment_matrix(payload: dict[str, object]) -> dict[str, object]:
    row_variable = _string(payload.get("row_variable"), default="intervention_minutes_per_robot_hour")
    column_variable = _string(payload.get("column_variable"), default="utilization")
    row_values = payload.get("row_values")
    column_values = payload.get("column_values")
    if not isinstance(row_values, list):
        row_values = [0.0, 5.0, 10.0, 15.0, 20.0]
    if not isinstance(column_values, list):
        column_values = [0.5, 0.6, 0.67, 0.75, 0.8]

    cells: list[list[dict[str, object]]] = []
    for row_value in row_values:
        row: list[dict[str, object]] = []
        for column_value in column_values:
            scenario_payload = dict(payload)
            scenario_payload[row_variable] = row_value
            scenario_payload[column_variable] = column_value
            result = evaluate_robotics_deployment(scenario_payload)
            outputs = result["outputs"]
            row.append(
                {
                    "row_value": row_value,
                    "column_value": column_value,
                    "effective_cost_per_human_equivalent_hour": outputs.get("effective_cost_per_human_equivalent_hour"),
                    "deployable_at_labor_parity": outputs.get("deployable_at_labor_parity"),
                    "customer_payback_months": outputs.get("customer_payback_months"),
                    "cost_advantage_per_human_equivalent_hour": outputs.get("cost_advantage_per_human_equivalent_hour"),
                    "audit": result["audit"],
                }
            )
        cells.append(row)

    return {
        "row_variable": row_variable,
        "column_variable": column_variable,
        "row_values": row_values,
        "column_values": column_values,
        "cells": cells,
    }


def robotics_deployment_model(payload: dict[str, object]) -> dict[str, object]:
    result = evaluate_robotics_deployment(payload)
    if bool(payload.get("include_matrix", False)):
        result["matrix"] = robotics_deployment_matrix(payload)
    return result
