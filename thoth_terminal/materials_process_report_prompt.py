from __future__ import annotations


MATERIALS_PROCESS_SUPER_REPORT_PROMPT = """# Thoth Materials / Process Super-Report Prompt

You are writing a deep technical investment research report for Thoth.

The target is a material, process input, process step, consumable, substrate, chemical, gas, specialty component, manufacturing method, or production constraint. Examples: ABF, glass-core substrates, quartz, helium, neon, photoresist, underfill, copper foil, antimony, specialty graphite, CMP slurry, EUV pellicles, thermal interface materials, low-CTE materials, hybrid bonding, advanced packaging substrates, or high-purity process gases.

This is not a generic materials primer. The report must explain why the material or process matters inside a real production stack, what physical property makes it hard to replace, what breaks when it is missing or inadequate, and who captures value when the value chain is forced to scale.

## Core Question

Does this material or process control, enable, relieve, or threaten a scarce, least substitutable, time-sensitive part of a value chain?

Do not force a bottleneck conclusion. A material/process can be a structural bottleneck, temporary supply crunch, extreme-demand beneficiary, forced-adoption substitute, hidden yield limiter, qualification gate, cost absorber, roadmap enabler, or long-term replacement. These categories can coexist.

## Evidence Labels

Use explicitly:
- **Supported**
- **Assumption**
- **Missing**
- **N/A**
- **N/I**

Mark uncertainty directly. Do not smooth over missing specs, yields, purity windows, supplier capacity, or qualification status.

## Sourcing Strategy

Preserve provenance:
- primary/company/regulatory/customer data
- technical papers / standards / patents
- supplier process disclosures
- customer qualification evidence
- institutional analyst synthesis
- vetted specialist analyst relay
- Thoth report memory
- unvetted commentary
- gap/speculation

For claims about purity, yield, qualification, capacity, substitution, pricing, customer adoption, roadmap, or failure mode, state what corroboration is still needed.

## 1. Abstract

Explain:
- what the material/process is,
- where it enters the production stack,
- why it matters now,
- whether the setup is bottleneck / supply crunch / forced adoption / extreme demand,
- the central physical constraint,
- and the investment consequence.

## 2. One-Sentence Technical Explanation

Write one precise sentence explaining the material/process without marketing language.

## 3. Where It Sits In The Stack

Map the exact insertion point.

Include upstream feedstocks, process step, downstream product, customer qualification owner, adjacent materials, equipment dependencies, test/inspection dependencies, and failure propagation.

Identify whether the constrained node is raw material, purification, formulation, conversion, deposition/coating, lamination, bonding, etch/clean, packaging, test, certification, or customer qualification.

## 4. Physical / Chemical / Mechanical Properties

This is the most important section.

Explain the properties that matter:
- purity
- defectivity
- particle size
- CTE / thermal expansion
- thermal conductivity
- dielectric constant
- loss tangent
- modulus / stiffness
- flatness
- warpage behavior
- adhesion
- moisture absorption
- chemical stability
- outgassing
- corrosion risk
- process temperature window
- lifetime / reliability

For each key property, define it, explain why it matters, explain what fails if it is wrong, and state evidence quality.

## 5. Process Mechanics

Explain how the material/process is made, handled, qualified, and used.

Cover feedstock path, conversion/refining/formulation, process equipment, cycle time, yield learning, rework/scrap risk, inspection/metrology, customer qualification, and production scaling.

Avoid saying "hard to manufacture" without explaining what is hard.

## 6. Bottleneck / Supply Crunch / Extreme Demand Classification

Use this table:

| Axis | Classification | Reason | Evidence Label |
|---|---|---|---|
| Structural bottleneck | Yes / No / Partial | | |
| Temporary supply crunch | Yes / No / Partial | | |
| Extreme demand beneficiary | Yes / No / Partial | | |
| Hidden yield limiter | Yes / No / Partial | | |
| Qualification gate | Yes / No / Partial | | |
| Forced-adoption substitute | Yes / No / Partial | | |
| Long-term replacement risk | Yes / No / Partial | | |

Then explain how the axes interact.

## 7. Forced Adoption Logic

Analyze whether the material/process may be adopted before it is fully mature because the incumbent path is breaking.

Do not evaluate adoption only by current cost, yield, or maturity. Customers may qualify an imperfect material/process if the incumbent cannot meet capacity, warpage, thermal behavior, mechanical stability, electrical performance, reliability, roadmap density, cycle time, or supplier diversification needs.

Classify adoption driver:
- better performance
- lack of incumbent capacity
- physical necessity
- yield workaround
- customer de-risking
- roadmap pressure
- geopolitical/supplier risk
- standards transition

If the new path has poor yield or high cost, explain why the old path may still be worse.

## 8. Substitution And Alternatives

Analyze substitutes seriously.

For each alternative:
- what property does it improve?
- what property gets worse?
- what process changes are required?
- what qualification burden exists?
- who would adopt first?
- what would prove adoption is real?
- what incumbent revenue pool is threatened?

Classify substitutes as near-term practical, partial mitigation, long-term threat, niche, technically interesting but commercially weak, or not credible.

## 9. Capacity, Expansion, And Lead Time

Explain capacity constraints.

Cover current supply base, expansion path, capex intensity, tool dependency, permitting/regulatory constraints, feedstock availability, purification bottlenecks, skilled labor, customer qualification time, and realistic ramp timing.

Separate nameplate capacity from qualified capacity.

## 10. Yield, Reliability, And Failure Cost

Explain how defects appear, where yield is lost, whether failures are visible early or latent, whether failure causes scrap, rework, field failure, or customer redesign, who bears the cost, and what metrics reveal stress.

## 11. Qualification And Customer Lock-In

Cover who qualifies the material/process, how long qualification takes, whether dual sourcing is required, switching cost, design-in depth, certification burden, and whether qualification creates pricing power.

## 12. Economics And Capture

Separate technical criticality, pricing power, margin capture, pass-through risk, capital intensity, supplier concentration, customer bargaining power, and shareholder economics.

A material can be essential but economically weak if customers can dual-source, specs are commoditized, or capacity expands quickly.

A material can be economically powerful if specs are narrow, qualification is deep, failure cost is high, supplier count is low, or expansion takes years.

## 13. Company Exposure Map

Classify companies by role:
- bottleneck owners
- exposed beneficiaries
- material suppliers
- process/tool suppliers
- formulation/conversion suppliers
- customers
- qualification gatekeepers
- substitutes
- likely losers
- ambiguous / needs research

For each, state role, exposure type, technical dependence, economic capture, evidence label, and missing information.

## 14. Public And Structural Roadmap

Separate:
- **public roadmap**: announced product/process/customer milestones.
- **structural roadmap**: what likely has to happen because physics, capacity, yield, or customer roadmaps require it.

Mark structural roadmap as Assumption unless directly supported.

## 15. Roadmap To 2030

Use a table:

| Period | Public Milestone | Structural Milestone | Constraint | Adoption Requirement | Winners | Losers | Evidence Label |
|---|---|---|---|---|---|---|---|

Focus on what must become true.

## 16. Bull Case

Write the strongest bull case:
- incumbent path breaks,
- substitute becomes necessary,
- qualification locks in suppliers,
- demand outruns capacity,
- failure cost supports pricing,
- roadmap makes the material/process more critical.

## 17. Bear Case

Write the strongest bear case:
- incumbent path keeps improving,
- new material/process yield remains poor,
- qualification takes too long,
- customers avoid redesign,
- capacity catches up,
- pricing power fades,
- substitute becomes niche.

Do not strawman the bear case.

## 18. Falsification Tests

List observable evidence that would change the conclusion:
- qualification wins/losses,
- yield data,
- customer adoption,
- pricing changes,
- capacity additions,
- process node roadmap changes,
- field reliability issues,
- substitute adoption,
- supplier capex,
- tool bottlenecks,
- standards changes.

## 19. Open Questions / Missing Information

Rank missing facts by importance.

For each, explain why it matters, where evidence might come from, affected reports/companies, and whether it blocks the thesis or refines it.

## 20. Report Update Instructions

| Update Target | Action | Reason | Priority |
|---|---|---|---|
| Company report | Add / revise / no change | | |
| Product report | Add / revise / no change | | |
| Bottleneck report | Add / revise / no change | | |
| Materials report | Add / revise / no change | | |
| Supply-chain object | Add / revise / no change | | |
| Watchlist / alert | Add / revise / no change | | |

## Final Rules

- Mechanism before market size.
- Physical properties before valuation.
- Qualified capacity before nameplate capacity.
- Mark Missing instead of inventing specs.
- Distinguish bottleneck, supply crunch, extreme demand, and forced adoption.
- Distinguish technical necessity from economic capture.
- Treat immature-but-necessary adoption seriously.
- Preserve provenance.
- Do not confuse a materials/process report with a company report.
- If adoption depends on incumbent failure, make that explicit.
- If the material/process is only a bridge, say so.
- If roadmap timing is decisive, make timing the center of the report.
"""
