# OpenAI public signals on robotics (2025–2026)

Compiled 2026-10-29 for the Amperes Failure Lab hackathon prototype.

> **Read this first.** Nothing in this document shows procurement intent, a buying process, or demand for
> Amperes. OpenAI has not endorsed, requested, evaluated, or expressed interest in Amperes, and nobody on this
> project has contacted OpenAI about it. Job postings describe who a company is hiring, not what it buys. When
> OpenAI's wording overlaps with Amperes' feature list, that is shared vocabulary, not validation.

## Method and access limits

- **openai.com/careers pages returned HTTP 403 to our fetcher.** We read the same postings from OpenAI's public
  Ashby job board (`https://jobs.ashbyhq.com/openai`, via the public API
  `https://api.ashbyhq.com/posting-api/job-board/openai`) on 2026-10-29. That snapshot had 812 postings. 15 were
  in team "Robotics", all in San Francisco, and one more robotics role ("Senior Manager, EHS - Robotics") was in
  the Workplace team. Dates below are the board's `publishedAt` values, which can reflect re-posting.
- **Closed postings** were read from mirrors on venture-capital job boards (Thrive Capital, Khosla Ventures). They
  copy OpenAI's postings, but we could not check their text against openai.com. All of them are marked
  "no longer accepting applications".
- **Press role counts conflict with ours.** TNW counted 21 robotics roles on 2026-09-18 and Entrepreneur counted 27
  on 2026-09-23; we counted 15 on 2026-10-29. Roles may have closed, or the outlets may have counted differently.
  We did not resolve the difference.
- **We could not fetch Business Insider directly** (the domain is blocked for our tools, and the AOL syndicated copy
  returned 404). The BI details below come from search-result summaries and secondary write-ups, so we hold them
  with lower confidence.
- **We found no openai.com blog post about robotics from 2025–2026.** Searches returned only secondary coverage,
  and because openai.com blocked our fetcher we could not browse its index directly.
- Quotes are ≤15 words, at most one per source. Everything else is paraphrased.

---

## A. Stated by OpenAI (primary sources: OpenAI postings and documents, or its executives' own words)

| # | Date | Source | Quote or paraphrase | What it plausibly implies (our inference) |
|---|---|---|---|---|
| A1 | 2026-08-04 | Field Engineer, Robotics — https://jobs.ashbyhq.com/openai/d8ebcb30-c789-4e75-bf9b-a1e3fb09dd2f | "many deployed workcells, changing configurations, and little tolerance for downtime". Paraphrase: the hire owns the daily health of the robotic workcells used for data acquisition. They diagnose failures across mechanics, electronics, controls, software, logs and configuration, keep tickets with issue history and escalation paths, write lightweight analysis scripts to find fleet trends and recurring failures, build lightweight monitoring and diagnostic tools, and write failure reports. | Workcell uptime and recurring-failure analysis are staffed priorities. **Counter-signal:** OpenAI expects this hire to build the tooling in-house. |
| A2 | 2026-06-05 | Robotics Software Engineer — https://jobs.ashbyhq.com/openai/fabfc6c5-57b5-49ba-aae9-e7dd5d7030ca (openai.com mirror: https://openai.com/careers/robotics-software-engineer-san-francisco/, 403 to us) | "expand our robotics data collection and evaluation program". Paraphrase: grow the data collection labs, sourcing and integrating hardware; build tooling that automates the evaluation of robot policies; lead the design of data collection, visualization and quality control processes. Rust or C++. | Data collection and policy evaluation sit in one program. The team designs its own QC processes. |
| A3 | 2026-09-10 | Senior Manager, EHS - Robotics — https://jobs.ashbyhq.com/openai/099374c6-3cbb-4964-9a1d-31e6e5b5dcd2 | "around-the-clock operations". Paraphrase: robotics operations are scaling quickly across multiple sites, with construction, commissioning and workforce expansion. | The physical footprint runs 24/7 and spans more than one site. |
| A4 | 2026-10-07 | Technical Program Manager, Robotics Infrastructure — https://jobs.ashbyhq.com/openai/fe7039ce-51e2-4cfa-9ea5-507eb7a0eee1 | Paraphrase: the Robotics point of contact for designing and building Robotics sites. Turns workflows into requirements for floor layout, utilities, network, environment and material flow, and runs commissioning and handover. | New robotics sites are being built. |
| A5 | 2026-06-05 | Research Program Manager, Data Acquisition — https://jobs.ashbyhq.com/openai/71004494-9a55-4ed5-b458-2ff475f0d881 | Paraphrase: sits on the Research Data Team. Scopes data needs with researchers, evaluates data opportunities and vendors, and uses SQL, Python, dashboards and simple tooling to track performance, quality and blockers. **This posting does not mention robotics.** | Data acquisition is run as a managed program. The tooling it expects is lightweight and internal. It may have nothing to do with robotics. |
| A6 | Posted "6+ months ago" per mirror; closed | Operations Manager, Data Acquisition (Robotics) — https://jobs.khoslaventures.com/companies/openai/jobs/72869971-operations-manager-data-acquisition (openai.com URL: https://openai.com/careers/operations-manager-data-acquisition-san-francisco/, 403 to us) | "own the end-to-end system that enables robotics data collection". Paraphrase: runs robotic stations, station software, a large managed workforce supplied by an outside services partner, and facilities as one system. Drives throughput, reliability and data quality, and sets standards across sites. Base pay $251K–$285K. | Throughput and data quality are management KPIs, and a vendor supplies the workforce. Operator effects on data quality likely matter. |
| A7 | 2026-06-27 per mirror; closed | Operations Program Manager - Robotics Data Acquisition — https://jobs.thrivecap.com/companies/openai/jobs/84415594-operations-program-manager-robotics-data-acquisition | "own the day-to-day operating rhythm in our data collection facilities". Paraphrase: tracks utilization, cycle time, throughput, downtime, operator productivity and data quality. Finds bottlenecks with time studies and capacity modelling, leads root-cause analysis of recurring issues, and coordinates calibration, configuration and rollouts. | This is the closest match to Amperes' planned Phase 3 metrics (yield, throughput, downtime, bottlenecks). That match is vocabulary only, not demand. |
| A8 | Posted "6+ months ago" per mirror; closed (one aggregator says 2025-06-18; unverified) | Data Lead, Robotics — https://jobs.thrivecap.com/companies/openai/jobs/52814927-data-lead-robotics | "validate and ensure the quality and diversity of robotics datasets". Paraphrase: sets strategy for robot-data quantity, quality and fidelity; builds real and simulated environments and tasks as training curricula; debugs the performance of deployed robots. | Dataset-quality tooling was a named responsibility of a senior internal hire. |
| A9 | 2026-09-08 | Control Systems Software Engineer, Robotics — https://jobs.ashbyhq.com/openai/60573bf4-13ad-4933-aba7-729f428e9f69 | Paraphrase: real-time control, state estimation, actuator control and whole-robot behaviour, working onsite with robots and with ML engineers on RL models. | In-house control stack, not only models for other companies' robots. |
| A10 | 2026-05-28 → 2026-10-23 | Hardware build and test roles, all team "Robotics" on the Ashby board: Electrical Engineer (05-28), TPM Actuators (08-20), PCB Layout (09-01), Mechanical Design – Motor Design (09-10), ME – Dynamometer & Actuator Testing Infrastructure (09-10), Manufacturing Engineer – Motors & Actuators (09-15), Lead Safety Engineer (10-06), ME/EE – PCBA Test Infrastructure (10-19, 10-22), Compute EE (10-21), Engineering Technician – Actuators (10-23) | Paraphrase: custom actuators and motors, test cells and dynamometers, PCBA test stations at contract manufacturers, build and test records with traceability, and a product safety strategy. | OpenAI is building its own robot hardware, so hardware test and bring-up data will grow. This is speculative for Amperes, which does not target hardware test data. |
| A11 | All current Robotics postings | Shared team blurb (e.g., A2) | Paraphrase: aims at general-purpose robotics and AGI-level intelligence in dynamic real-world settings, across a broad range of robot form factors. | The stated goal is broad, with no product or customer named. |
| A12 | January 2026 (PDF metadata 2026-01-14) | RFP for U.S. Hardware Manufacturing — https://cdn.openai.com/pdf/rfp-for-us-hardware-manufacturing.pdf | Paraphrase: seeks US manufacturing partners for consumer devices, data centers and robotics. The robotics scope covers actuator and electromechanical modules, precision bearings, harmonic drives, gearboxes and motors, magnets and power electronics. Initial proposals were due June 2026; vendor selection is planned for March 2027. | This is a hardware supply-chain RFP. **It is not a software or data-tooling procurement and is not relevant to buying Amperes.** |
| A13 | June 2025 (the page shows no date; the date comes from coverage, e.g. https://thezvi.substack.com/p/the-dream-of-a-gentle-singularity) | Sam Altman, "The Gentle Singularity" — https://blog.samaltman.com/the-gentle-singularity (CEO's personal blog, not openai.com) | "2027 may see the arrival of robots that can do tasks in the real world." | Executive-level timeline framing, hedged with "may". |
| A14 | 2025-01-10 | Caitlin Kalinowski on X, read via press (the original post was not fetched): https://techcrunch.com/2025/01/10/new-openai-job-listings-reveal-its-robotics-plans/ | Paraphrase: the first robotics hardware roles were opening, and OpenAI would build its own robots with a custom sensor suite. Postings at the time mentioned general-purpose, adaptive robots and, in one mechanical role, high-volume (1M+ unit) manufacturing experience. | This marks the restart of hardware robotics in early 2025. |

## B. Reported by press (not confirmed by OpenAI unless stated)

| # | Date | Source | Paraphrase | Confidence / notes |
|---|---|---|---|---|
| B1 | Jan 2026 (date inferred from URL, not confirmed) | Business Insider (Grace Kay). AOL syndication https://www.aol.com/news/inside-openais-renewed-push-robotics-101701659.html returned 404 to us. Summary: https://briefly-prod.herokuapp.com/anchor/Artificial_intelligence/story/inside-openais-renewed-push-into-robotics | A San Francisco lab launched in Feb 2025 and has more than quadrupled. About 100 data collectors use 3D-printed GELLO controllers to teleoperate Franka arms on household tasks, reportedly around the clock. A second lab is planned in Richmond, CA. OpenAI declined to comment. | Medium-low. Unnamed insiders, and we read it second-hand. |
| B2 | 2026-01-15 | Bloomberg — https://www.bloomberg.com/news/articles/2026-01-15/openai-seeks-us-based-suppliers-for-planned-robotics-ai-device-push | OpenAI is seeking US suppliers for robotics and devices (this is the A12 RFP). Spend is not disclosed. | Not fetched; search summary only. Consistent with A12. |
| B3 | 2026-03 | Hoodline — https://hoodline.com/2026/03/openai-moves-in-on-richmond-waterfront-with-giant-ford-point-warehouse-lease/ | OpenAI leased a warehouse of about 202,000 sq ft in Richmond. OpenAI has not stated what it will be used for. | Not fetched; search summary only. |
| B4 | 2026-03-07/08 | Engadget — https://www.engadget.com/ai/openais-head-of-robotics-resigns-following-deal-with-the-department-of-defense-195918777.html | Robotics hardware lead Caitlin Kalinowski resigned over the Department of Defense deal. OpenAI told Engadget it had no plans to replace her. | Widely reported. Who owns robotics tooling decisions now is unknown to us. |
| B5 | 2026-09-18 | TNW — https://thenextweb.com/news/openai-robotics-engineer-salaries-500000 | Counted 21 robotics roles. The top-paid ($380K–$500K) was an ML role building the training-data pipeline, not working on robots. The writer frames robotics as a data problem (that is the writer's analysis). | Medium. That posting was not on the Ashby board on 2026-10-29, so we could not read it. |
| B6 | 2026-09-23 | Entrepreneur — https://www.entrepreneur.com/business-news/openai-will-pay-you-a-500000-salary-plus-equity-for-this-futuristic-job | Counted 27 robotics openings, up from 11 in May, including data collection roles and a manager for the data collection facilities. The top role builds infrastructure to process, store and move training data. | Medium. Counts conflict with B5 and with our own count. |
| B7 | 2026-10-17 | Engadget — https://www.engadget.com/2285040/openai-humanoid-robot-sam-altman-statement-real-challenge/ | On the *Sources* podcast, Altman said OpenAI will build a humanoid and that the "brain" matters more than the body. To *Fortune* he predicted a demo in 2027. No design, price or release date has been announced. | Medium-high for the statements themselves. Dates for the two interviews differ across outlets. |

**Seen but excluded (could not verify):** a claim that OpenAI "formally announced its robotics division on
May 31, 2026" (one secondary source, no primary found), and the full text of the $500K robotics data-infrastructure
posting (it was no longer on the board).

## C. Our inference (not stated by OpenAI)

1. **Real-world teleoperated data collection is being run as an industrial operation.** The evidence: workcells,
   stations, a managed vendor workforce, multiple sites, 24/7 shifts (A1, A3, A4, A6, A7, B1).
2. **The operations KPIs named in the postings** (utilization, cycle time, throughput, downtime, data quality,
   root-cause analysis of recurring issues) overlap Amperes' planned Phase 3 outputs. This is the strongest
   topical overlap, and it is still not evidence of demand.
3. **Policy evaluation and data collection are run together** (A2). Amperes' sim-based failure lab is far simpler
   than this scope.
4. **Strongest counter-signal: OpenAI is hiring people to build this tooling itself.** A1 says build lightweight
   tools and write analysis scripts, A2 says design QC processes, A5 says simple tooling and dashboards, and A8
   says build dataset-quality tooling. The postings also stress secure-systems access and data security. An
   external tool would therefore face both build-vs-buy and data-access barriers.
5. **The robotics hardware lead left in March 2026, and press reports no replacement** (B4). We do not know who would
   own or approve an external evaluation, and we should not guess.

## Most decision-relevant signals for Amperes

1. A1 Field Engineer (workcell health, recurring failures, in-house scripts).
2. A2 Robotics Software Engineer (data collection + evaluation program, QC processes).
3. A7 Operations Program Manager - Robotics Data Acquisition (utilization / cycle time / downtime / data quality).
4. A8 Data Lead, Robotics (dataset quality and diversity tooling).
5. A3 Senior Manager, EHS - Robotics (24/7, multi-site scale), read alongside B1.

None of these shows that OpenAI would buy or try an external tool.
