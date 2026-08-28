description: Systematically research a US nonprofit from public records — definitive 990 revenue via ProPublica/IRS, the donation platform via the donate page's embedded services, leadership, programs, and SaaS stack — ending in ranked operational-friction hypotheses. Use whenever the user names a nonprofit, .org domain, or EIN and asks about its budget, size, funding, donors, software, inefficiencies, or fit as a prospect.

# Nonprofit Discovery — execute these steps; do NOT infer what you can look up

You have a `web_research` tool. Its result includes the page's visible text AND an
`[embedded third-party services & links]` list of the external hosts the page uses.
That list is how you identify SaaS providers — the visible text won't name them.

Work the steps below and, for every claim, mark it **CONFIRMED** (you looked it up)
or **INFERRED** (an educated guess). Prefer looking up over inferring.

## 1. Definitive financials — never guess a budget range
- Fetch ProPublica Nonprofit Explorer for the org:
  `https://projects.propublica.org/nonprofits/search?q=<ORG NAME>`, then open the
  org's result page.
- Report the **exact** total revenue, total expenses, and net assets, with the
  **fiscal year**, from the most recent Form 990.
- Only if you truly cannot retrieve a 990 may you say so — do NOT substitute an
  inferred dollar range for a figure you could have looked up.

## 2. Donation / giving platform — identify it by name
- Fetch the donate page (`/donate`, `/give`, `/donate-now`, `/support`) and the homepage.
- Read the `[embedded third-party services & links]` list and match hosts to vendors:
  - `etapestry.com`, `app.etapestry.com`, `blackbaud`, `bbnc`, `bbox`, `sphere` → **Blackbaud / eTapestry**
  - `classy.org` → Classy · `givebutter.com` → Givebutter · `donorbox.org` → Donorbox
  - `neoncrm.com`, `z2systems` → Neon CRM · `qgiv.com` → Qgiv · `bloomerang.co` → Bloomerang
  - `donorperfect`, `networkforgood`, `fundraiseup.com`, `every.org`, `paypal.com`, `stripe.com`
- Name the platform explicitly and cite the host you matched. Only if NO third-party
  giving host appears may you conclude "no embedded platform — likely manual/check."

## 3. Leadership & programs
- Board, executive team, program lines, licensing/accreditation, service area.

## 4. SaaS stack signals
- From the embedded-services lists (across pages), job postings, and page footers,
  identify: donor CRM, EHR/case management, payroll, accounting, ATS.

## 5. Ranked operational-friction hypotheses
- List the highest-leverage operational frictions first, each tied to a component
  in the catalog, with a one-line rationale.
