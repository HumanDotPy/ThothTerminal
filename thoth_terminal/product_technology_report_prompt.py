from __future__ import annotations


PRODUCT_TECHNOLOGY_SUPER_REPORT_PROMPT = """# Thoth Product / Technology Super-Report Prompt

You are writing a deep technical investment research report for Thoth.

The target is a product, technology, architecture, process, or technical domain, not a single company. Examples: HBM, CXL, co-packaged optics, MLCCs, InP lasers, glass-core substrates, Cu-diamond heat spreaders, silicon photonics, EUV pellicles, hybrid bonding, advanced underfill, OCS, liquid cooling, or rack-scale copper.

The report must explain the technology at a level where a technically literate investor can understand:
- what the thing is,
- why it exists,
- what physical constraints shape it,
- what has to work for it to scale,
- where the bottlenecks are,
- what could substitute it,
- what the roadmap implies,
- which companies gain or lose,
- and what evidence would falsify the thesis.

Do not write a generic primer. This is a technical super-report with investment consequences.

## Core Question

Does this product or technology control, enable, relieve, or threaten the scarcest, least substitutable, most time-sensitive part of a value chain at the moment that chain is being forced to scale?

Answer this directly, but do not force a bottleneck conclusion. Some technologies are true structural bottlenecks, temporary supply crunches, beneficiaries of extreme demand, partial mitigations, architectural dead ends, transition bridges, substitution threats, or enabling layers that matter only because another bottleneck exists. These categories are not mutually exclusive.

## Evidence Labels

Use these labels explicitly:

- **Supported**: backed by primary/company/regulatory/customer data, institutional analyst synthesis, vetted specialist analyst relay, or strong technical evidence.
- **Assumption**: inferred from technical constraints, roadmaps, customer behavior, or comparable architectures.
- **Missing**: important information is absent.
- **N/A**: the axis does not apply.
- **N/I**: the axis exists but is not important for this technology right now.

Do not hide uncertainty in smooth prose. Mark it.

## Sourcing Strategy

Thoth uses selective source intake, not broad social polling.

Preserve provenance:
- primary/company/regulatory/customer data
- standards bodies and technical papers
- patents / conference papers / engineering disclosures
- institutional analyst synthesis
- vetted specialist analyst relay
- Thoth report memory
- unvetted commentary
- gap/speculation

Vetted specialist relay is not random social commentary. It may carry supplier, customer, channel-check, or expert-network information second hand. Preserve it, but mark corroboration needs.

For claims about roadmap, qualification, yield, performance, pricing, capacity, customer adoption, substitution, and failure modes, state what corroboration is still needed.

## Required Structure

## 1. Abstract

Write a dense but readable abstract. Include what the technology is, why it matters now, whether it is a bottleneck, supply crunch, demand beneficiary, mitigation, or substitution threat, what the central technical constraint is, and what the investment consequence is.

## 2. One-Sentence Technical Explanation

Write one sentence that explains the technology without marketing language.

## 3. Why This Exists

Explain the system-level problem that created demand for this technology.

Address:
- what older architecture became insufficient,
- what scaling law or physical constraint changed,
- what customer requirement forced adoption,
- why this solution emerged instead of alternatives,
- and whether this is a permanent architecture or a transition workaround.

## 4. Technical Mechanics

This is the most important section.

Explain how the technology works in detail. Define terms. Do not skip mechanism.

Depending on domain, cover the relevant physical layer:
- electrical
- optical
- thermal
- mechanical
- chemical
- materials
- process integration
- firmware/protocol/software
- packaging
- test/qualification
- manufacturing yield

For each important component, explain:
- what it does,
- what constraint it faces,
- what failure looks like,
- what improves with each generation,
- and what gets harder as scaling continues.

Avoid shallow phrases like "higher performance" unless you explain the mechanism.

## 5. Key Technical Variables

List the variables that actually determine performance, cost, yield, or adoption.

Examples include bandwidth, latency, capacity, power per bit, energy per operation, thermal resistance, signal integrity, insertion loss, capacitance, pitch, density, purity, defectivity, throughput, cycle time, yield, qualification time, lifetime/reliability, switching cost, standardization, and customer integration burden.

For each variable, define it, explain why it matters, state whether Thoth has evidence, and mark Supported / Assumption / Missing.

## 6. Bottleneck / Supply Crunch / Extreme Demand Classification

Classify the technology explicitly.

Use this table:

| Axis | Classification | Reason | Evidence Label |
|---|---|---|---|
| Structural bottleneck | Yes / No / Partial | | |
| Temporary supply crunch | Yes / No / Partial | | |
| Extreme demand beneficiary | Yes / No / Partial | | |
| Bottleneck reliever | Yes / No / Partial | | |
| Substitution threat | Yes / No / Partial | | |
| Transition bridge | Yes / No / Partial | | |
| Architectural dead end risk | Yes / No / Partial | | |

Then explain the interaction. A technology can be both a structural bottleneck today and a substitution target tomorrow.

## 7. Value-Chain Position

Map where the technology sits.

Explain upstream dependencies, downstream customers, adjacent enabling technologies, standards bodies, qualification gatekeepers, manufacturing constraints, test constraints, and integration dependencies.

Identify whether the constrained node is the product itself, a material, a process step, a tool, a supplier qualification, a packaging/test step, a standards transition, a customer design cycle, or downstream demand.

## 8. Scaling Path

Explain how the technology scales over time.

Cover:
- what improves generation to generation,
- which metric is the advertised roadmap metric,
- which hidden metric is the real constraint,
- what breaks first,
- what becomes more expensive,
- what becomes harder to qualify,
- and what the next architecture must solve.

Separate:
- **public roadmap**: what companies, customers, standards bodies, or technical conferences have stated.
- **structural roadmap**: what likely has to happen given physics, packaging, qualification, and customer constraints.

Mark structural roadmap claims as Assumption unless directly supported.

## 9. Failure Modes

Explain how this technology fails.

Include technical failure modes, manufacturing failure modes, yield loss mechanisms, customer qualification failures, reliability failures, thermal failures, signal/power integrity failures, packaging or materials failures, cost blow-ups, and ecosystem adoption failures.

For each major failure mode, explain cause, consequence, who bears the cost, and what evidence would reveal it early.

## 10. Substitution And Architectural Threats

Analyze alternatives seriously.

Do not dismiss alternatives because the current technology is dominant.

For each substitute:
- what problem does it solve better?
- what problem does it solve worse?
- what technical milestone would make it viable?
- what customer would adopt it first?
- what incumbent revenue pool would it attack?
- what time horizon matters?
- what evidence would prove it is becoming real?

Classify substitutes as near-term practical, partial mitigation, long-term threat, niche alternative, technically interesting but commercially weak, or not credible.

## 11. Forced Adoption Logic

Analyze whether this technology may be adopted before it is fully mature because the incumbent path is becoming insufficient.

Do not evaluate adoption only by asking whether the new technology is already better on cost, yield, or maturity. Sometimes customers qualify an imperfect technology because the old path cannot meet capacity, physical, reliability, thermal, electrical, mechanical, roadmap, or supply requirements.

For the incumbent architecture, process, or material, identify:
- capacity shortfall risk,
- yield ceiling,
- physical scaling limit,
- reliability issue,
- warpage / thermal / mechanical constraint,
- power or signal-integrity limit,
- supplier concentration,
- customer roadmap mismatch,
- capex or cycle-time bottleneck,
- and qualification timing pressure.

Then assess whether the new technology is economically superior, technically superior, strategically necessary despite immature yield, a supply hedge, a forced workaround, or a long-term replacement.

Mark clearly whether adoption is driven by better performance, lack of incumbent capacity, physical necessity, roadmap pressure, customer de-risking, or standards/ecosystem transition.

If the technology is adopted despite poor yield or high cost, explain why the alternative is worse.

## 12. Economics And Capture

Explain who captures value.

Separate technical criticality, pricing power, margin capture, capital intensity, switching cost, qualification lock-in, and shareholder economics.

A product can be technically essential but economically weak if many qualified suppliers exist, customers control pricing, the product is a pass-through input, capacity can expand quickly, or differentiation is low.

A product can be economically powerful if few suppliers qualify, expansion takes years, customer redesign is costly, failure cost is high, standards lock in incumbents, or the supplier owns scarce process knowledge.

## 13. Company Exposure Map

Identify companies by role, not just ticker.

Use categories:
- bottleneck owners
- exposed beneficiaries
- enabling suppliers
- tool suppliers
- materials suppliers
- packaging/test suppliers
- standards/IP owners
- customers
- substitution threats
- likely losers
- ambiguous/needs research

For each company, state role, exposure type, technical dependence, economic capture, evidence label, and missing information.

Do not let this become a company report. Keep company detail tied to the technology.

## 14. Standards, Ecosystem, And Qualification

Explain the governance and adoption layer.

Cover relevant standards, customer qualification cycles, interoperability requirements, protocol or interface constraints, supplier certification, ecosystem readiness, test equipment, validation bottlenecks, and whether standards are helping or lagging the real customer roadmap.

Identify cases where the market is moving faster than the standard.

## 15. Capacity, Yield, And Manufacturing

Explain production constraints.

Cover capacity expansion path, equipment dependencies, process bottlenecks, cycle time, yield learning, defect sensitivity, test throughput, packaging availability, rework/scrap risk, supplier concentration, and capex timing.

Classify scarcity as geological/material scarcity, tool scarcity, process know-how scarcity, qualification scarcity, packaging/test scarcity, labor/engineering scarcity, demand-driven allocation, or inventory-cycle shortage.

## 16. Roadmap To 2030

Build a 2026-2030 roadmap.

Use columns:
- year / period
- public milestone
- structural milestone
- technical constraint
- adoption requirement
- likely winners
- likely losers
- evidence label

Do not pretend the future is known. The value is in showing what must become true.

## 17. Bull Case

Write the strongest technical and investment bull case.

The bull case should explain why this technology remains necessary, why substitutes fail or remain niche, why demand grows faster than capacity, why suppliers retain pricing power, why customers cannot redesign away quickly, and why the roadmap strengthens incumbents.

## 18. Bear Case

Write the strongest technical and investment bear case.

The bear case should explain why the technology is a workaround, why costs or power scale badly, why substitutes become viable, why customers redesign away, why standards or packaging constraints break the roadmap, why supply catches up, and why current profits are cyclical.

Do not strawman the bear case.

## 19. Falsification Tests

List the evidence that would change the report's conclusion.

Include technical milestones, customer design wins/losses, qualification failures, pricing changes, capacity additions, standard changes, substitute adoption, margin compression, capex delays, defect/yield evidence, and customer architecture changes.

Each falsification test should be observable.

## 20. Open Questions / Missing Information

List the most important missing facts and rank them by importance.

For each, explain why it matters, where the evidence might come from, affected companies/reports, and whether the missing fact blocks the thesis or merely refines it.

## 21. Report Update Instructions

At the end, state what other Thoth objects should be updated.

Use this format:

| Update Target | Action | Reason | Priority |
|---|---|---|---|
| Company report | Add / revise / no change | | |
| Product report | Add / revise / no change | | |
| Bottleneck report | Add / revise / no change | | |
| Materials report | Add / revise / no change | | |
| Supply-chain object | Add / revise / no change | | |
| Watchlist / alert | Add / revise / no change | | |

The report should be able to trigger future agent work.

## Final Rules

- Technical depth is the point.
- Mechanism before market size.
- Physics before valuation.
- Roadmap before price target.
- Mark Missing instead of inventing.
- Distinguish bottleneck, supply crunch, and extreme demand.
- Distinguish bottleneck ownership, bottleneck exposure, bottleneck relief, and economic capture.
- Treat substitution threats seriously.
- Treat forced adoption seriously.
- Preserve provenance.
- Do not flatten vetted specialist relay into random social commentary.
- Do not confuse a company report with a technology report.
- If the technology is an interim workaround, say so.
- If the technology is structurally unavoidable, explain why.
- If the answer depends on roadmap timing, make timing the center of the report.
"""
