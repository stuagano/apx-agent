# demos/gyansys-staffing/generate_data.py
"""Deterministic synthetic Salesforce + Replicon data for the GyanSys demo.

No external deps — stdlib only. `generate()` returns plain dicts so the loader
(Task 3) can write them to UC however it likes.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

REFERENCE_DATE = date(2026, 7, 1)

REGIONS = ["US", "India", "Philippines", "South America"]
SKILLS = [
    "Databricks", "PySpark", "Spark", "SQL", "Python", "Azure", "SAP",
    "Data Engineering", "Machine Learning", "Power BI", "Delta Lake",
    "Unity Catalog", "ETL", "Tableau", "dbt", "Kafka",
]
ROLES = [
    "Data Engineer", "Senior Data Engineer", "ML Engineer", "Data Architect",
    "BI Developer", "Analytics Lead", "Platform Engineer",
]
STAGES = ["Prospecting", "Qualification", "Proposal", "Negotiation",
          "Closed Won", "Closed Lost"]
OPEN_STAGES = {"Prospecting", "Qualification", "Proposal", "Negotiation"}
ACCOUNTS = [f"{a} {s}" for a in
            ["Northwind", "Globex", "Initech", "Umbrella", "Acme", "Soylent",
             "Hooli", "Stark", "Wayne", "Wonka"]
            for s in ["Corp", "Industries"]]
FIRST = ["Aarav", "Priya", "Maria", "Juan", "John", "Emily", "Wei", "Ana",
         "Carlos", "Divya", "Rahul", "Sofia", "Liam", "Noah", "Mia", "Raj"]
LAST = ["Patel", "Sharma", "Garcia", "Santos", "Smith", "Cruz", "Reyes",
        "Mendoza", "Kumar", "Silva", "Johnson", "Lopez", "Chen", "Das"]
CERTS = ["Databricks Certified Data Engineer Associate",
         "Databricks Certified ML Associate", "Azure Data Engineer",
         "AWS Solutions Architect", "(none)"]


@dataclass
class GeneratedData:
    opportunities: list[dict]
    people: list[dict]


def _name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def _people(rng: random.Random) -> list[dict]:
    people: list[dict] = []
    for i in range(200):
        region = rng.choice(REGIONS)
        title = rng.choice(ROLES)
        skills = rng.sample(SKILLS, k=rng.randint(3, 6))
        availability = float(rng.choice([0, 10, 20, 25, 40, 50, 60, 75, 90, 100]))
        people.append({
            "person_id": f"P{1000 + i}",
            "name": _name(rng),
            "title": title,
            "practice": "Analytics",
            "region": region,
            "skills": ", ".join(skills),
            "certifications": rng.choice(CERTS),
            "availability_pct": availability,
            "cost_rate": float(rng.randint(60, 220)),
            "current_project": rng.choice(["Bench", "Acme Migration",
                                           "Globex DW", "Internal R&D"]),
        })

    # Planted scarcity: India is tight on Databricks. EVERY India person who can
    # do Databricks work is near-fully allocated (the bandwidth story). Applying
    # this to all India+Databricks people — not just a few — is what makes the
    # "India is tight on Databricks" answer unambiguous.
    india_dbx = [p for p in people
                 if p["region"] == "India" and "Databricks" in p["skills"]]
    if not india_dbx:  # guarantee at least one for the demo
        forced = next(p for p in people if p["region"] == "India")
        forced["skills"] = "Databricks, PySpark, " + forced["skills"]
        india_dbx = [forced]
    for p in india_dbx:
        p["availability_pct"] = float(rng.choice([0, 10, 20]))
    return people


def _opportunities(rng: random.Random) -> list[dict]:
    opps: list[dict] = []
    for i in range(75):
        stage = rng.choice(STAGES)
        created = REFERENCE_DATE - timedelta(days=rng.randint(20, 300))
        # most opps have recent activity; some don't
        last_activity = REFERENCE_DATE - timedelta(days=rng.randint(0, 25))
        role = rng.choice(ROLES)
        req_skills = rng.sample(SKILLS, k=rng.randint(2, 4))
        opps.append({
            "opportunity_id": f"OPP{2000 + i}",
            "name": f"{rng.choice(ACCOUNTS)} — {role} engagement",
            "account_name": rng.choice(ACCOUNTS),
            "stage": stage,
            "amount": float(rng.randint(20, 500) * 1000),
            "probability": rng.randint(10, 90),
            "close_date": (REFERENCE_DATE + timedelta(days=rng.randint(10, 120))).isoformat(),
            "created_date": created.isoformat(),
            "last_activity_date": last_activity,  # date obj; serialized in loader
            "region": rng.choice(REGIONS),
            "required_role": role,
            "required_skills": ", ".join(req_skills),
            "stall_reason": "",
        })

    # Planted stalled opps: 4 high-value, open-stage, no activity > 30 days.
    reasons = ["Awaiting customer security review", "Budget approval pending",
               "Champion left the account", "Stuck on legal redlines"]
    for j in range(4):
        o = opps[j]
        o["stage"] = "Proposal"
        o["amount"] = float(rng.randint(120, 480) * 1000)
        o["last_activity_date"] = REFERENCE_DATE - timedelta(days=rng.randint(45, 120))
        o["stall_reason"] = reasons[j]
    return opps


def generate(seed: int = 42,
             reference_date: date = REFERENCE_DATE) -> GeneratedData:
    rng = random.Random(seed)
    return GeneratedData(
        opportunities=_opportunities(rng),
        people=_people(rng),
    )


if __name__ == "__main__":
    data = generate()
    stalled = [o for o in data.opportunities
               if o["stall_reason"] and o["stage"] in OPEN_STAGES]
    print(f"people={len(data.people)} opportunities={len(data.opportunities)} "
          f"stalled={len(stalled)}")
