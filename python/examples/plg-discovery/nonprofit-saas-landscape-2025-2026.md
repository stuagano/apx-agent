# The Small-to-Medium Nonprofit Software Landscape (2025–2026)

*Research compiled August 2026 for the "agentic discovery → custom app suite" project. Prototypical target: [Urban Gleaners](https://urbangleaners.org) (Portland, OR food-rescue nonprofit).*

---

## How to read this report

This document has three jobs:

1. **Map the SaaS ecosystem** small/medium nonprofits actually use, category by category, with pricing and market position.
2. **Explain the adoption dynamics** — why nonprofits pick what they pick (spoiler: donated/discounted access dominates feature comparison).
3. **Translate the landscape into build implications** for an agentic system that discovers a nonprofit's needs and assembles a custom suite (final section).

A running theme: for organizations under ~$5M budget, **price and "did it come free through TechSoup / Google / Salesforce" beat feature depth**, and the market has bifurcated into (a) consolidator platforms (Bonterra, Blackbaud, Bloomerang) chasing mid/enterprise and (b) a vibrant free/low-cost tier (Zeffy, Give Lively, Givebutter, Little Green Light, SignUpGenius) competing for the small end.

> **Confidence & freshness note.** Pricing below is approximate as of mid-2026 and changes frequently — most enterprise/mid-market tools are quote-only. Consolidation is ongoing and fast; product names lag reality. Treat every dollar figure as "verify before quoting to a client." Items flagged 🚩 need independent confirmation.

---

## 1. Executive summary — the 10 things that matter

1. **The nonprofit tech stack is ~8 functional domains**, rarely one tool: donor CRM, online fundraising, grant seeking, program/case management, volunteer management, events/auctions, communications/marketing, and back-office (accounting, HR, productivity, website).
2. **Small nonprofits layer 3–5 point tools**; there is no true all-in-one that covers everything well. The closest to "suite" plays are Bloomerang (donor + volunteer + events) and Bonterra (enterprise), plus the Salesforce/Microsoft ecosystems.
3. **Free access is the #1 adoption driver**, ahead of features. Google Workspace (donated), Microsoft 365 (10 free seats), Salesforce Power of Us (10 free licenses), and TechSoup (the donation aggregator) shape what tools get chosen more than any product comparison.
4. **A genuinely free fundraising tier now exists and is competitive**: **Zeffy** (0% fees, tip-funded), **Give Lively** (philanthropically funded), and **Givebutter** (free tier) are rapidly taking the small-nonprofit market.
5. **Massive consolidation** (2021–2026): Bonterra absorbed EveryAction/Social Solutions/Network for Good/Apricot/Penelope/DonorDrive; Bloomerang absorbed Kindful/InitLive/Qgiv; GoFundMe bought Classy (→ "GoFundMe Pro"); CaseWorthy absorbed ClientTrack/Eccovia; Aplos bought Keela.
6. **Pricing opacity is a market signal.** Transparent pricing correlates with small-nonprofit fit (Little Green Light, DonorSnap, Zeffy, SignUpGenius, Auctria, Instrumentl). "Contact sales" correlates with enterprise positioning that systematically excludes small orgs.
7. **Board-management software is an accessibility desert** for small nonprofits — every serious tool is quote-only enterprise SaaS with no free tier.
8. **Grant tooling is bifurcated** into grant *seeking* (nonprofits applying — Instrumentl leads) vs. grant *making* (funders giving — Fluxx leads). Few tools serve both.
9. **Vertical software matters for operational nonprofits.** A food-rescue org like Urban Gleaners needs food-logistics, route optimization, and pantry/inventory tools that the generic fundraising stack doesn't touch (MealConnect, Food Rescue Hero, Link2Feed, Routific/OptimoRoute).
10. **AI is now table stakes** in vendor marketing (Fluxx "Finn," Instrumentl proposal drafting, CaseWorthy "Cara," Bloomerang "Prospect AI," Govenda "Gabii") — but mostly bolted onto legacy workflows, which is precisely the opening for an agentic build-a-suite approach.

---

## 2. The adoption context: discount & donation programs (the real gatekeepers)

Before any product comparison, understand that most small nonprofits assemble their stack from **donated and discounted access**. These programs, not feature sheets, drive adoption.

| Program | What you get | Why it dominates adoption |
|---|---|---|
| **TechSoup** | Donation/discount marketplace: Microsoft, Google, Adobe, Cisco, Salesforce discounts + 500 tools; apply annually | The hub for small-nonprofit procurement; validates 501(c)(3) status once, unlocks many vendors |
| **Google for Nonprofits** | Google Workspace Business Standard donated (~$14/user value); **Google Ad Grants ~$10k/mo** in search ads 🚩(amount/policy shifts); YouTube Nonprofit; Cloud credits | Ad Grants is a keystone donor-acquisition subsidy; donated Workspace makes Google the default office suite |
| **Microsoft for Nonprofits (Tech for Social Impact)** | Microsoft 365 free for up to **10 users**; Dynamics 365 ~50% off; Azure credits | Zero-cost M365 removes the office-suite barrier |
| **Salesforce Power of Us** | **10 free** Salesforce licenses (~$1–2k/mo value) + discounts | Anchors mid/larger and technically-capable nonprofits into the Salesforce ecosystem |
| **Meta for Nonprofits** | ~$7k/mo advertising credits (FB/IG) 🚩 | Awareness/acquisition subsidy |
| **Canva for Nonprofits** | Free Canva Teams (up to ~5) | Near-universal for social/design |
| **HubSpot for Nonprofits** | Free/discounted CRM + marketing automation | Growing among marketing-forward small orgs |
| **Zoom for Nonprofits** | ~40% off (most use free tier) | Video default |

**Implication for the project:** any "we'll build you a custom suite" pitch competes not against $200/mo SaaS but against *free* incumbents the org already has (Google Workspace, a free Zeffy page, a Mailchimp free tier). The wedge is **integration and fit**, not raw capability.

---

## 3. Donor / constituent management (CRM)

The system of record for donors, gifts, and relationships. This is the anchor tool most nonprofits agonize over.

### Small-nonprofit tier (transparent, budget-friendly)
| Product | ~2026 price | Notes |
|---|---|---|
| **Little Green Light** | $45/mo (2.5k records) → $135/mo (50k) | Cheapest cost/record, unlimited users, simple; 200k record cap; independent |
| **DonorSnap** | $50/mo (1k contacts) → $125/mo (10k) + $200 setup | Very affordable, unlimited users, QuickBooks integration |
| **Bloomerang** | CRM from ~$125/mo; Giving Platform ~$242/mo | Priced by *records not seats* (unlimited users); transparent; strong small/mid adoption; the leading "suite-ish" pick |
| **Neon CRM** | ~$134/mo (1k) → $379+/mo (10k+) | Unlimited users, no transaction fees, strong comms |
| **DonorPerfect** | from ~$99/mo, scales by DB size | Long-standing; QuickBooks + Constant Contact; custom quotes at scale |
| **Keela** | ~$134–379/mo | AI + wealth screening; **acquired by Aplos (2023)** — verify current bundling 🚩 |

### Enterprise tier (quote-only)
- **Blackbaud Raiser's Edge NXT** — gold-standard enterprise CRM; typically $1k+/mo; for $20M+ orgs.
- **Salesforce Nonprofit Cloud / NPSP** — 10 free licenses via Power of Us, then ~$100–300/user/mo; infinitely customizable but needs technical staff.
- **Virtuous** — "responsive fundraising" CRM + AI; mid/enterprise; opaque pricing.

**Absorbed/legacy:** Kindful → Bloomerang (2021); Network for Good → Bonterra (2022).

---

## 4. Online fundraising / donation platforms

Donation pages, recurring giving, peer-to-peer, text-to-give. Increasingly free at the small end.

### Free / ultra-low-cost (winning the small market)
| Product | Model | Notes |
|---|---|---|
| **Zeffy** | **$0** platform *and* $0 processing; funded by optional donor tips | Fastest-growing among bootstrapped orgs; no lock-in; fewer enterprise features |
| **Give Lively** | **$0** — philanthropically funded | No P2P/limited customization; endorsed by major funders; longevity depends on funding model |
| **Givebutter** | Free tier (0% if tips on; 3% flat if not); Plus from $29/mo | Largest genuinely-free feature set; strong UX; unlimited users on free |
| **Donorbox** | Free tier (2.95% + $0.30); Pro $150/mo (1.75%) | Versatile — recurring, crypto, stock; transparent tiers |
| **Funraise** | Free <$1M revenue (5% platform fee); paid from ~$99/mo | Genuinely free for small orgs |
| **Anedot** | Transaction-based (~2.2% + processing) | No subscription |

### Mid-market → enterprise
- **Classy → "GoFundMe Pro"** (GoFundMe acquired Classy, 2022) — multi-channel + P2P; free tier <$1M, custom above; taps GoFundMe's 190M+ donor base.
- **DonorDrive** (Bonterra) — enterprise P2P/livestream/mobile; $1k+/mo.

**Consolidated/legacy:** Qgiv → Bloomerang (2024); Fundly → largely folded into SignUpGenius; **Snowball** status unclear 🚩.

---

## 5. Grant seeking (nonprofits applying for grants)

| Tool | ~2026 price | Notes |
|---|---|---|
| **Instrumentl** | $179+/mo | Market leader; AI matching across 450k+ funders; **post-award tracking** (a differentiator); claims ~75% time savings |
| **GrantHub** (Foundant) | $349/mo + ~$500 impl | Transparent; integrated tracking; weaker discovery AI than Instrumentl |
| **Grantable** | Free / $50 / $150/mo (50% nonprofit discount) | AI writing focus (not discovery); ultra-low cost |
| **GrantWatch** | Free + token pricing ($12–95) | 12k+ verified grants; token model can confuse |
| **GrantStation** | Custom 🚩 | 150k+ funder profiles; strong education content |
| **Candid / Foundation Directory** | Free + Pro (undisclosed) | 1.8M+ org database; more reference than workflow |
| **Grantmakers.io** | **Free**, no login, open-source | 374k+ funder profiles from IRS 990 data |
| **Submittable** (seeker side) | Custom | Dual-sided (seeker + funder) |

*(For completeness — grant **making** software for funders: **Fluxx** (leader), **Foundant GLM** (~$9,900/yr, unlimited users), **SmartSimple**, **Submittable**, **Blackbaud Grantmaking**. Likely out of scope unless a target nonprofit re-grants funds.)*

---

## 6. Program / case management (service-delivery nonprofits)

Client intake, service tracking, outcomes/impact reporting. Critical for direct-service orgs (including food assistance).

| Tool | Pricing | Notes |
|---|---|---|
| **CharityTracker** | Custom, no onboarding fees | 20k+ users; established, stable, simple; light on AI |
| **CaseWorthy** | Custom | **Absorbed ClientTrack/Eccovia (Feb 2025)**; modern cloud; "Cara" AI copilot; HMIS/gov compliance |
| **Apricot by Bonterra** | Custom | Outcome tracking; part of Bonterra ecosystem |
| **Salesforce for Nonprofits** | ~$1k–3k/mo (discounted) | Endlessly customizable; heavy implementation |
| **Penelope** (Bonterra) | Custom | **Legacy status** — supported but new buyers steered elsewhere 🚩 |

**Impact/outcomes measurement** is a fragmented market with no purpose-built leader — mostly survey/analytics tools (SurveySparrow, SurveyCake) and Candid/GuideStar data tools adapted for nonprofits.

---

## 7. Volunteer management

| Product | ~2026 price | Notes |
|---|---|---|
| **SignUpGenius** | **Free** (paid tiers optional) | Best free option for small orgs; sign-ups, scheduling, reminders, payments |
| **Track It Forward** | Free <25 volunteers; $10.60–29.60/mo | Transparent, affordable hour-tracking |
| **Bloomerang Volunteer** | $119/mo standalone; $242/mo with CRM | Unlimited users; from the InitLive acquisition |
| **Better Impact** | Custom 🚩 | Established; opaque pricing |
| **VolunteerHub** | $143–288/mo+ | Enterprise-grade; overkill for small orgs |
| **Rosterfy / Timecounts** | Custom enterprise | Larger orgs |
| **VolunteerMatch** | Free listings | Recruitment marketplace; **merged into Idealist (early 2025)** |

**Defunct/absorbed:** InitLive → Bloomerang (2023).

---

## 8. Events, galas & auctions

| Product | ~2026 price | Notes |
|---|---|---|
| **Eventbrite** | Free hosting + 3.7% + $1.79/ticket | Industry standard; 30k+ nonprofits; best free option |
| **Auctria** | Free tier → ~$375/yr Emerald | Transparent; excellent for auctions/raffles |
| **GiveSmart / OneCause / Handbid** | Custom (quote-only) | Enterprise gala/auction platforms; $5B+ raised collectively; opaque |
| **Virtuous Raise** (formerly RaiseDonors) | Custom | Enterprise |

**Defunct/unreachable at research time:** ClickBid, RaiseDonors (→ Virtuous), Greater Giving (403/offline) 🚩.

---

## 9. Board management (governance)

**Accessibility desert — all quote-only, no free tiers.**

- **Govenda** — unlimited users, "Gabii" AI; positions toward smaller orgs (most promising, but verify pricing 🚩).
- **Boardable** — AI transcription; per-user; demo required.
- **BoardEffect** & **Diligent Boards** — part of Diligent's enterprise suite.
- **OnBoard** — domain unreachable at research time 🚩.

For small nonprofits, board work today usually happens in Google Drive + email + a shared calendar — a clear gap an agentic suite could fill cheaply.

---

## 10. Back-office & horizontal tools

### Accounting / finance
QuickBooks Online dominates small nonprofits (familiarity + TechSoup discount). **Aplos** (purpose-built fund accounting + donor tools), **Sage Intacct**/**Blackbaud Financial Edge NXT** (mid/large), **Xero**, **Wave** (free, for micro-orgs), **Bill.com** (AP automation).

### HR / payroll
**Gusto** dominates small nonprofits (25–40% discount + all-in-one). ADP/Paychex for mid-market; Rippling for tech-forward orgs; BambooHR for HR/talent.

### Marketing / email / texting
**Mailchimp** (free tier is the default; 15% nonprofit discount), **Constant Contact** (25% off, better nonprofit support), **Brevo**, **Flodesk**; texting via **EZ Texting**/**Tatango**.

### Productivity / collaboration
**Google Workspace** and **Microsoft 365** dominate via donation programs. Project/knowledge tools with 50% nonprofit discounts: **Asana, Notion, Airtable**; plus **Slack, Zoom** (40% off), **Trello, Monday**.

### Website / CMS / forms / e-sign
**WordPress** (free, dominant), **Squarespace** (50% off, "professional look"), **Wix**; **Jotform** (50% off) for forms; **DocuSign/SignNow** for e-signature.

### Scheduling / calendaring
**Google Calendar** (bundled, near-universal), **Calendly** (50% off), When2Meet (free) at the bottom.

---

## 11. Vertical spotlight: food rescue (Urban Gleaners context)

### Urban Gleaners profile (inferred)
- **Mission:** "Food For All" — rescue surplus food and redistribute to fight both waste and insecurity; Portland OR metro (Multnomah/Washington counties).
- **Programs:** gleaning pickups (restaurants, grocers, corporate campuses); **20+ free food markets**; food prep; community education.
- **Scale:** ~80,000+ lbs/month; ~8,500 families/week; small team (10–30 range, hiring a Development & Communications Director as of 2026); volunteer count not public. *(Figures from public site; verify.)*
- **Inferred systems needs:** food-donor management, perishable inventory/logistics, volunteer scheduling for pickups/deliveries/markets, **route/driver coordination**, financial-donor & grant management, impact reporting (lbs rescued, meals served, families reached), food-safety/compliance logs, and optional client/family tracking.

### Food-rescue vertical software
| Category | Tools | Notes |
|---|---|---|
| **Rescue matching/logistics** | **MealConnect** (Feeding America, free), **Food Rescue Hero** (412 Food Rescue; purpose-built, mobile volunteer app, impact dashboards), **Replate**, **ChowMatch** | Often free/grant-funded for food rescue |
| **Pantry/food-bank inventory & distribution** | **Link2Feed**, **Oasis Insight** (Simon Solutions), **PantrySoft**, **Primarius** 🚩 | Client intake + inventory + reporting; TEFAP/CSFP compliance |
| **Route optimization** | **Routific** (nonprofit case studies), **OptimoRoute** (nonprofit testimonials), **Onfleet**, **Circuit** 🚩 | For multi-stop pickup/delivery routing |
| **Volunteer scheduling (shift-based)** | **SignUpGenius** (free), **Food Rescue Hero** | Generic vs. purpose-built |

**Takeaway:** the food-rescue vertical is under-served and fragmented — most orgs stitch together a free volunteer tool + a route optimizer + spreadsheets + a generic donor CRM. That gap is exactly the kind of thing a custom-built suite could consolidate.

---

## 12. Market consolidation map (2021–2026)

- **Bonterra** (2022, Apax-backed): EveryAction + Social Solutions + CyberGrants + Network for Good → then Apricot, Penelope, DonorDrive, Mobilize, CiviCore. The largest consolidator; enterprise/large focus.
- **Bloomerang**: Kindful (2021) + InitLive (2023) + Qgiv (2024). Building an integrated small/mid suite with unlimited-user, record-based pricing.
- **Blackbaud**: Raiser's Edge NXT, Financial Edge NXT, JustGiving, Altru, YourCause, Luminate, ResearchPoint. Legacy enterprise leader.
- **GoFundMe**: acquired Classy (2022) → rebranded "GoFundMe Pro."
- **CaseWorthy**: absorbed ClientTrack/Eccovia (Feb 2025).
- **Aplos**: acquired Keela (2023) — pairs fund accounting with donor CRM.
- **Idealist**: merged VolunteerMatch (early 2025).

---

## 13. Adoption by nonprofit size (quick reference)

| Budget | Decision driver | Typical stack |
|---|---|---|
| **Micro (<$500k)** | Cost is ~90% of decision | Wave/QuickBooks, Mailchimp free, WordPress, Google (donated), Zeffy/Give Lively, SignUpGenius, When2Meet |
| **Small ($500k–$2M)** | ~50/50 cost + fit | Bloomerang or Little Green Light/DonorSnap, Givebutter/Donorbox, Gusto, QuickBooks (TechSoup), Google Workspace, Asana (50% off), Instrumentl (if grant-reliant) |
| **Mid ($2M–$10M)** | Features ~60% | Salesforce (Power of Us) or Neon/DonorPerfect, GoFundMe Pro/Classy, Microsoft 365, Aplos/Intacct, CaseWorthy/Apricot (if service delivery), Fluxx (if grantmaking) |
| **Large ($10M+)** | Enterprise capability | Blackbaud RE NXT, Salesforce, Workday/ADP, enterprise Intacct, DonorDrive |

---

## 14. Synthesis for the project: building an agentic "discover → assemble a suite" system

The landscape maps cleanly onto a **discovery ontology** and a **build surface**. Below is how this research feeds the actual product.

### 14.1 The functional domains to discover
A nonprofit's needs decompose into these modules. Discovery dialog should probe each and score relevance:

1. **Donor/constituent CRM** — who gives, gift history, relationships. *(Almost always needed.)*
2. **Online fundraising** — donation pages, recurring, P2P, events.
3. **Grant seeking** — pipeline of applications, deadlines, reporting *(needed if grant-reliant — ask "% of revenue from grants").*
4. **Program/case/service management** — only for direct-service orgs; tracks clients & outcomes.
5. **Volunteer management** — scheduling, hours, communications *(scales with volunteer count).*
6. **Events/auctions** — galas, ticketing *(episodic).*
7. **Communications/marketing** — email, social, texting.
8. **Back-office** — accounting, HR/payroll, documents, calendar, board governance.
9. **Vertical/operational** — the domain-specific module (for food rescue: logistics, inventory, routing). *This is where custom build beats buying.*

### 14.2 Discovery questions that actually segment (from adoption patterns)
- **Annual budget & staff/volunteer counts** → sizing tier → complexity ceiling.
- **Revenue mix** (individual donors / grants / events / earned) → which modules dominate.
- **Direct-service vs. advocacy/support** → whether case management is in scope.
- **Existing tools & what came free** (Google/Microsoft/Salesforce grants, TechSoup) → integration constraints, not greenfield.
- **The one vertical workflow they do daily** (for Urban Gleaners: rescue → route → distribute → report pounds) → the highest-value custom module.
- **Compliance surface** (food safety, HMIS, grant reporting, 990) → data model & audit requirements.

### 14.3 Where an agentic build wins vs. buying
- **Integration/consolidation:** small orgs run 3–5 disconnected tools + spreadsheets. A unified data model is the value.
- **Vertical gaps:** food rescue, board management, and impact measurement are under-served / expensive. Strong candidates for custom modules.
- **The "free incumbent" reality:** don't rebuild Mailchimp or Google Calendar; **integrate** them. Build the connective tissue and the vertical workflow; wrap the commodity pieces.
- **Reporting/impact:** a persistent pain (lbs rescued, meals served, funder reports) with no dominant tool — high-leverage to auto-generate.

### 14.4 Where buying still wins (don't rebuild)
- **Accounting** (regulatory/audit trust → QuickBooks/Aplos), **payroll** (Gusto), **payment processing/donation rails** (Stripe under the hood; or just embed Zeffy/Givebutter), **email deliverability** (Mailchimp/Constant Contact). Reduce scope by treating these as integrations.

### 14.5 A "reference stack" the system could benchmark against
For a small direct-service nonprofit like Urban Gleaners, the honest incumbent alternative is roughly: *Bloomerang or Little Green Light (donors) + Zeffy/Givebutter (donations) + Instrumentl (grants) + SignUpGenius + Routific (volunteers/routing) + a food-rescue tool (MealConnect/Food Rescue Hero) + QuickBooks + Gusto + Google Workspace + spreadsheets for impact.* The pitch for a custom suite is collapsing that into one coherent system with the food-logistics workflow as the centerpiece.

---

## 15. Caveats & verification checklist

- **Pricing** is approximate (mid-2026) and changes quarterly; enterprise tools are quote-only. Verify before quoting.
- **Consolidation** is fast — confirm current ownership/branding (esp. Classy/GoFundMe Pro, Keela/Aplos, Penelope).
- **Discount-program terms** (Google Ad Grants amounts, Microsoft's 10-seat program) have been restructured recently 🚩 — check vendor pages.
- **Defunct/uncertain at research time:** ClickBid, OnBoard, Greater Giving, Snowball, Primarius, Circuit, Careit.
- **Urban Gleaners specifics** (budget, staff, volunteer counts) are inferred from the public site — confirm directly.

---

## 16. Key sources

**Discount/donation programs:** TechSoup, Google for Nonprofits, Microsoft Tech for Social Impact, Salesforce Power of Us, Meta/Canva/HubSpot for Nonprofits.
**CRM & fundraising:** Bloomerang, Little Green Light, DonorSnap, Neon CRM, DonorPerfect, Blackbaud, Salesforce.org, Virtuous, Zeffy, Give Lively, Givebutter, Donorbox, Funraise, GoFundMe Pro/Classy.
**Grants & programs:** Instrumentl, Foundant, GrantWatch, Grantmakers.io, Candid/GuideStar, Fluxx, Submittable, CaseWorthy, CharityTracker, Bonterra/Apricot.
**Volunteer/events/board:** SignUpGenius, Track It Forward, VolunteerHub, Better Impact, Eventbrite, Auctria, OneCause, GiveSmart, Govenda, Boardable, BoardEffect, Diligent.
**Back-office:** QuickBooks, Aplos, Sage Intacct, Xero, Wave, Gusto, Mailchimp, Constant Contact, Asana, Notion, Airtable, WordPress, Squarespace, Jotform, DocuSign.
**Food-rescue vertical:** MealConnect (Feeding America), Food Rescue Hero (412 Food Rescue), Replate, ChowMatch, Link2Feed, Oasis Insight, PantrySoft, Routific, OptimoRoute, Onfleet.
**Industry references:** Capterra, Software Advice, G2, NTEN, Nonprofit Tech for Good.

*(Full per-vendor links captured in the underlying research; available on request.)*
