# GyanSys Staffing Coworker (demo)

Read-only Databricks demo: matches Salesforce opportunities to Replicon people
by skill similarity, surfaces stalled pipeline and staffing bandwidth.

**As built (live):** workspace profile `fevm-serverless-stable-qh44kx`, UC location
`serverless_stable_qh44kx_catalog.gyansys_staffing`, VS endpoint `gyansys_demo_vs`.
Deployed App (Databricks SSO): https://gyansys-staffing-7474652869938903.aws.databricksapps.com —
RUNNING and answering grounded questions. (The deploy's `/readyz` gate reports a
false-negative; the apps runtime serves `/health`, which returns 200.)
(The design target was `gyansys_demo.staffing` on `fe-stable`, but that principal
lacked catalog/schema/Vector-Search rights there — catalog/schema/profile are
workspace-specific constants in `load_to_uc.py` / `setup_vector_index.py` /
`gyansys-staffing.yaml`; change them for other environments.)

## Run order
1. `python generate_data.py` then `python load_to_uc.py`   (synthetic data → UC)
2. `python setup_vector_index.py`                            (people skill-profile VS index; ~20 min first build)
3. `uv run apx-agent agents run gyansys-staffing.yaml`       (local dev UI)
4. `uv run apx-agent agents deploy gyansys-staffing.yaml --target apps`

See ARCHITECTURE.md for the deep-dive talking points.

## Verified demo answers (local run against serverless-stable)

- **"Best-fit available people for the highest-value stalled opportunity?"** →
  names the stalled opp (champion left the account) and shortlists people with
  region, skills, availability %, and cost rate, plus a leadership recommendation
  (e.g. Rahul Reyes, 90% available, $76/hr). Vector match + SQL both fire.
- **"Which opportunities are stalled, and why?"** → lists the 4 planted stalled
  opps with real IDs/amounts and their stall reasons (security review, legal
  redlines, budget pending, lost champion).
- **"How much availability for Databricks work in India?"** → grounded
  availability breakdown across the India bench (certs + Databricks-ecosystem
  skills), with high-availability and fully-booked people called out.
