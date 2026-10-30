# Phase 3 customer discovery: interview script and scoring sheet

For robotics **data-acquisition engineers, research engineers, operations / field engineers, and TPMs**.
Assumptions being tested: [CUSTOMER_DISCOVERY.md](CUSTOMER_DISCOVERY.md). Context:
[COMPETITIVE_GAP.md](COMPETITIVE_GAP.md).

## Interviewer rules

- **No pitching or demo until the final 5 minutes** (optional). Don't name Amperes features in questions.
- Ask about **past events and current behaviour**, not hypotheticals ("would you use…"). Compliments and "that's
  interesting" are not evidence.
- Let silences run. Ask "why?", "how do you know?", and "what happened next?"
- **Never ask for confidential data during the call.** Any sample data goes only through the interviewee's own
  approval process, after the call, in a form they choose.
- Record verbatim phrases where you can. Mark anything you inferred as your inference.
- 40 minutes. Get consent to take notes, and confirm what may be quoted (anonymised by default).

## 0. Context (3 min)

1. What's your role, and what does a normal week look like?
2. Roughly how many robots, stations or workcells does your team run? How many episodes or hours do you collect
   per day? (Ranges are fine.)
3. What is the data used for: training, evaluation, both, something else?

## 1. Recent concrete incident (15 min, the core of the interview)

4. Think of the **most recent time** collected data or an evaluation run turned out to be unusable, wrong, or
   misleading. When was it?
5. Walk me through it from the start. What happened first?
6. How was it noticed? Who noticed it, and how long after the data was collected?
7. What did you look at to work out what happened? Which logs, dashboards, videos, tickets, people?
8. What **couldn't** you determine from the records? What did you have to guess, or re-run to find out?
9. How long did it take to resolve, in people and hours? What did it cost: lost data, delayed training, re-collection?
10. What changed afterwards? Did anyone add a check, a test, or a process step?
11. How typical was that incident? How often does something like it happen?

## 2. Failure modes and detection (5 min)

12. What are the most frequent operational problems in collection or evaluation? (Hardware, sensors, calibration,
    operators, software, config, environment, labels.)
13. For each one, how is it detected today: automatically, by a person, or only later, during training?
14. Which problems are found latest, and what does finding them late cost?

## 3. Data and records (5 min)

15. What gets recorded per episode? Which formats and storage, and how long is it kept?
16. Are operational events recorded (faults, recoveries, maintenance, config changes, shift handovers)? Where, and
    can they be joined to episodes?
17. How are success/failure labels produced (operator, script, verifier, none)? How reliable are they, and how do
    you know?

## 4. Measurement and workflow (4 min)

18. How do you measure data quality today? Is there a number someone reports? Who sees it?
19. How do you track incidents: which tool, who owns them, what statuses do they go through?
20. When a failure is understood, how does it become a regression test, if it does? Who writes the test, and how
    long does it take?
21. What have you already built or scripted for any of this? What would you change about it?

## 5. Integration, evaluation, ownership (3 min)

22. If a tool touched this data, what would it have to integrate with? What security or deployment constraints
    apply (on-prem, no cloud, access reviews)?
23. Suppose you tried something new here for two weeks. What would it have to **show** for you to keep using it?
    What would make you stop?
24. Who owns this problem? Who would have to approve an evaluation, and who would approve spending money? Has your
    team evaluated or bought a tool in this area before? What happened?

## Persona add-ons (pick 2–3)

- **Data-acquisition engineer:** of the hours collected, how much ends up being used, and how is that decided? What slows an
  operator's shift most?
- **Research engineer:** when a policy regresses, how do you tell bad data from a bad model? Describe the last time.
- **Operations / field engineer:** walk through your last ticket on a station that was down. Where did the
  diagnosis time go?
- **TPM:** which metric about collection or evaluation do you report upward? How is it computed, and do you trust it?

## Close (5 min)

25. Is there anything I should have asked but didn't?
26. Who else deals with this that I should talk to? (Ask for an introduction.)
27. *(Only now, optional)* Show a 3-minute walk-through, then ask: "What, if anything, here matches what you
    described, and what doesn't?"
28. Would you be willing to take a follow-up step? Possibilities: share an approved, sanitised sample; review a
    report on it; introduce the person who approves evaluations. Record exactly what they offer.

---

## Scoring sheet (fill in within 24 h; one per interview)

Score each dimension **separately**. Never add stated interest to the other dimensions.

| Field | Entry |
|---|---|
| Interview ID / date / persona / org type | |
| Incident described? (Y/N) · how long ago · frequency they claim | |
| Time cost of that incident (people × hours) · consequence | |
| Current tooling or workaround (named) | |
| Verbatim quote on the pain (≤ 2) | |

| Dimension | 0 | 1 | 2 | 3 | Score | Evidence (what they said or did) |
|---|---|---|---|---|---|---|
| **Stated interest** | None or polite | "Interesting" | Asks detailed questions | Asks when they can try it | | |
| **Observed behaviour** (problem is real now) | No recent incident | Incident, but no action taken | Built scripts, spreadsheets or a process for it | Dedicated people or budget already spent on it | | |
| **Willingness to share approved sample data** | Declines | "Maybe later" with no step | Names the approval path or owner | Started approval, or shared a sanitised sample/schema | | |
| **Willingness to pay** | No budget, no owner | Owner exists, no budget discussed | Names a budget holder and a buying process | Agrees to a paid pilot or a scoped evaluation with a decision date | | |

**Commitments made** (introduction, sample, follow-up date): ____  **Our inferences, kept separate:** ____

Interpretation: high stated interest with low observed behaviour is a **false positive**. Weight decisions on
observed behaviour, data sharing and payment. Weigh them across interviews, not one at a time.
