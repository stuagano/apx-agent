"""Databricks Industry Data Models -> APX OKF jumpstart.

Consumes the public ``lakehouse-industry-data-models`` ``model.json`` shape and
emits the same ``.apx/okf`` bundle APX already uses for DataAgent grounding.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json
import re

from ._okf import OKFDocument, OKF_VERSION, dump_schema_cache, okf_manifest


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _md(value: Any) -> str:
    return _text(value).replace("|", r"\|").replace("\n", " ")


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip().lower()).strip("_")
    return slug or "industry_model"


def _attributes(product: dict[str, Any]) -> list[dict[str, str]]:
    attrs = product.get("attributes")
    if not isinstance(attrs, list):
        return []
    out: list[dict[str, str]] = []
    for attr in attrs:
        if not isinstance(attr, dict):
            continue
        name = _text(attr.get("column_name")) or _text(attr.get("name"))
        if not name:
            continue
        out.append({
            "name": name,
            "type": _text(attr.get("type")),
            "description": _text(attr.get("description")),
            "term": _text(attr.get("business_glossary_term")),
            "tags": _text(attr.get("tags")),
            "foreign_key_to": _text(attr.get("foreign_key_to")),
            "references": _text(attr.get("references")),
        })
    return out


def _domains(model: dict[str, Any]) -> list[dict[str, Any]]:
    raw = model.get("domains")
    return [d for d in raw if isinstance(d, dict)] if isinstance(raw, list) else []


def _domain_products(domain: dict[str, Any]) -> list[dict[str, Any]]:
    raw = domain.get("products")
    return [p for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []


def _table_name(product: dict[str, Any]) -> str:
    return _slug(_text(product.get("table_name")) or _text(product.get("name")))


def _dedupe_table_name(table: str, domain: str, seen: set[str]) -> str:
    if table not in seen:
        seen.add(table)
        return table
    candidate = f"{_slug(domain)}__{table}"
    i = 2
    while candidate in seen:
        candidate = f"{_slug(domain)}__{table}_{i}"
        i += 1
    seen.add(candidate)
    return candidate


def industry_model_manifest(
    model_json: Path | str,
    *,
    catalog: str,
    schema: str,
) -> dict[str, Any]:
    """Return APX's derived ``schema.json`` shape from an industry ``model.json``."""
    data = json.loads(Path(model_json).read_text())
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, dict):
        raise ValueError("model.json must contain a top-level object at key 'model'")
    seen: set[str] = set()
    tables: dict[str, list[str]] = {}
    for domain in _domains(model):
        domain_name = _text(domain.get("name"))
        for product in _domain_products(domain):
            table = _dedupe_table_name(_table_name(product), domain_name, seen)
            tables[table] = [
                f"{a['name']}({a['type']})"
                for a in _attributes(product)
            ]
    return {"catalog": catalog, "schema": schema, "tables": tables}


def _tables_section(table_names: list[str]) -> str:
    return "".join(f"* [{name}](../tables/{name}.md)\n" for name in table_names)


def _glossary_section(model: dict[str, Any], limit: int = 40) -> str:
    terms: dict[str, str] = {}
    for domain in _domains(model):
        for product in _domain_products(domain):
            for attr in _attributes(product):
                term = attr["term"]
                if term and term not in terms:
                    terms[term] = attr["description"] or f"Column `{attr['name']}`."
                if len(terms) >= limit:
                    break
            if len(terms) >= limit:
                break
        if len(terms) >= limit:
            break
    if not terms:
        return ""
    body = ["\n# Glossary\n"]
    for term, definition in terms.items():
        body.append(f"### {_md(term)}\n{_md(definition)}\n\n")
    return "".join(body).rstrip() + "\n"


def _metric_views_section(model: dict[str, Any], limit: int = 20) -> str:
    raw = model.get("metric_views")
    views = [v for v in raw if isinstance(v, dict)] if isinstance(raw, list) else []
    if not views:
        return ""
    lines = ["\n# Metric Views\n"]
    for view in views[:limit]:
        name = _md(view.get("view_name"))
        desc = _md(view.get("description"))
        owner = _md(view.get("owner_product"))
        suffix = f" - {desc}" if desc else ""
        owner_text = f" (`{owner}`)" if owner else ""
        lines.append(f"* `{name}`{owner_text}{suffix}\n")
    if len(views) > limit:
        lines.append(f"* (+{len(views) - limit} more metric views)\n")
    return "".join(lines)


def _dataset_body(model: dict[str, Any], table_names: list[str]) -> str:
    overview = _text(model.get("description"))
    processes = _text(model.get("core_business_processes"))
    domains = _text(model.get("data_domains"))
    jargon = _text(model.get("common_business_jargons"))
    sections = ["# Overview\n", overview or "Databricks industry data model.", "\n\n"]
    if processes:
        sections.append(f"# Business Processes\n{processes}\n\n")
    if domains:
        sections.append(f"# Domains\n{domains}\n\n")
    if jargon:
        sections.append(f"# Business Jargon\n{jargon}\n\n")
    sections.append("# Tables\n")
    sections.append(_tables_section(table_names))
    sections.append(_metric_views_section(model))
    sections.append(_glossary_section(model))
    return "".join(sections).rstrip() + "\n"


def _table_body(domain: dict[str, Any], product: dict[str, Any]) -> str:
    attrs = _attributes(product)
    rows = []
    for attr in attrs:
        desc = attr["description"]
        extras = []
        if attr["term"]:
            extras.append(f"term: {attr['term']}")
        if attr["tags"]:
            extras.append(f"tags: {attr['tags']}")
        if attr["foreign_key_to"]:
            extras.append(f"fk: {attr['foreign_key_to']}")
        detail = desc
        if extras:
            detail = f"{detail} ({'; '.join(extras)})" if detail else "; ".join(extras)
        rows.append(f"| `{attr['name']}` | {_md(attr['type'])} | {_md(detail)} |\n")

    joins = [
        f"* `{attr['name']}` -> `{attr['foreign_key_to']}`\n"
        for attr in attrs
        if attr["foreign_key_to"]
    ]
    overview = _text(product.get("description")) or f"{_table_name(product)} table."
    bits = [
        "# Overview\n",
        overview,
        "\n\n",
        "# Source Ontology\n",
        f"* Domain: `{_text(domain.get('name'))}`\n",
        f"* Subdomain: `{_text(product.get('subdomain'))}`\n",
        f"* Product type: `{_text(product.get('type'))}`\n",
        f"* Data type: `{_text(product.get('data_type'))}`\n",
        "\n# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n",
        *rows,
    ]
    if joins:
        bits.extend(["\n# Joins\n", *joins])
    return "".join(bits).rstrip() + "\n"


def _product_question_pair(
    table: str,
    domain: dict[str, Any],
    product: dict[str, Any],
    *,
    catalog: str,
    schema: str,
) -> dict[str, Any]:
    desc = _text(product.get("description")) or f"{table} table."
    domain_name = _text(domain.get("name"))
    pk = _text(product.get("primary_key"))
    attrs = _attributes(product)
    fk_count = sum(1 for attr in attrs if attr["foreign_key_to"])
    answer_bits = [desc.split(".")[0].strip() or desc]
    if pk:
        answer_bits.append(f"Primary key: `{pk}`.")
    if fk_count:
        answer_bits.append(f"{fk_count} declared foreign key relationship{'s' if fk_count != 1 else ''}.")
    return {
        "question": f"What does {table} tell us in the {domain_name or 'industry'} model?",
        "answer": " ".join(answer_bits),
        "table": f"{catalog}.{schema}.{table}",
        "domain": domain_name,
        "subdomain": _text(product.get("subdomain")),
        "source": "databricks-industry-solutions/lakehouse-industry-data-models",
    }


def industry_model_question_pairs(
    model_json: Path | str,
    *,
    catalog: str,
    schema: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Bounded ontology-derived question/answer pairs for examples/topology.

    These are not data answers. They are model-literacy pairs: what a source
    product means, which table backs it, and why that node exists in the agent's
    data graph. They are useful as topology metadata, starter questions, or eval
    seeds before real customer data exists.
    """
    data = json.loads(Path(model_json).read_text())
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, dict):
        raise ValueError("model.json must contain a top-level object at key 'model'")
    seen: set[str] = set()
    pairs: list[dict[str, Any]] = []
    for domain in _domains(model):
        domain_name = _text(domain.get("name"))
        for product in _domain_products(domain):
            table = _dedupe_table_name(_table_name(product), domain_name, seen)
            pairs.append(_product_question_pair(
                table,
                domain,
                product,
                catalog=catalog,
                schema=schema,
            ))
            if len(pairs) >= limit:
                return pairs
    return pairs


def industry_model_topology_metadata(
    model_json: Path | str,
    *,
    catalog: str,
    schema: str,
    limit_per_table: int = 1,
) -> dict[str, dict[str, Any]]:
    """Return ``node_metadata`` for :func:`apx_agent.annotate_topology`.

    Keys match APX topology resource node ids for UC table resources:
    ``uc:<catalog>.<schema>.<table>``.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for pair in industry_model_question_pairs(
        model_json,
        catalog=catalog,
        schema=schema,
        limit=10_000,
    ):
        table = str(pair["table"])
        node_id = f"uc:{table}"
        entry = grouped.setdefault(
            node_id,
            {
                "purpose": pair["answer"],
                "domain": pair["domain"],
                "subdomain": pair["subdomain"],
                "question_answer_pairs": [],
            },
        )
        if len(entry["question_answer_pairs"]) < limit_per_table:
            entry["question_answer_pairs"].append({
                "question": pair["question"],
                "answer": pair["answer"],
                "source": pair["source"],
            })
    return grouped


def write_industry_model_okf_bundle(
    model_json: Path | str,
    okf_root: Path | str,
    *,
    catalog: str,
    schema: str,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Write an enriched OKF bundle from a Databricks industry ``model.json``."""
    data = json.loads(Path(model_json).read_text())
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, dict):
        raise ValueError("model.json must contain a top-level object at key 'model'")

    root = Path(okf_root)
    (root / "datasets").mkdir(parents=True, exist_ok=True)
    (root / "tables").mkdir(parents=True, exist_ok=True)
    ts = timestamp or datetime.now(timezone.utc).isoformat()

    seen: set[str] = set()
    table_docs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for domain in _domains(model):
        domain_name = _text(domain.get("name"))
        for product in _domain_products(domain):
            table = _dedupe_table_name(_table_name(product), domain_name, seen)
            table_docs.append((table, domain, product))

    industry = _text(model.get("industry_alignment")) or _text(model.get("name")) or schema
    dataset = OKFDocument(
        frontmatter={
            "type": "Databricks Schema",
            "title": schema,
            "description": f"{industry} industry ontology jumpstart.",
            "resource": f"{catalog}.{schema}",
            "catalog": catalog,
            "schema": schema,
            "source": "databricks-industry-solutions/lakehouse-industry-data-models",
            "timestamp": ts,
        },
        body=_dataset_body(model, [name for name, _, _ in table_docs]),
    )
    dataset.validate()
    (root / "datasets" / f"{schema}.md").write_text(dataset.serialize())

    for table, domain, product in table_docs:
        doc = OKFDocument(
            frontmatter={
                "type": "Unity Catalog Table",
                "title": table,
                "description": _text(product.get("description")) or f"{table} table.",
                "resource": f"{catalog}.{schema}.{table}",
                "source_domain": _text(domain.get("name")),
                "source_table": _text(product.get("table_name")) or _text(product.get("name")),
                "timestamp": ts,
            },
            body=_table_body(domain, product),
        )
        doc.validate()
        (root / "tables" / f"{table}.md").write_text(doc.serialize())

    (root / "tables" / "index.md").write_text(
        "# Tables\n" + "".join(f"* [{name}]({name}.md)\n" for name, _, _ in table_docs)
    )
    (root / "index.md").write_text(
        f'---\nokf_version: "{OKF_VERSION}"\n---\n\n'
        "# Subdirectories\n* [datasets](datasets/)\n* [tables](tables/)\n"
    )
    manifest = okf_manifest(root)
    if manifest is None:
        raise ValueError(f"generated OKF bundle did not parse: {root}")
    return manifest


def write_industry_model_apx(
    model_json: Path | str,
    apx_dir: Path | str,
    *,
    catalog: str,
    schema: str,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Write ``.apx/okf`` plus derived schema and topology metadata caches."""
    apx = Path(apx_dir)
    manifest = write_industry_model_okf_bundle(
        model_json,
        apx / "okf",
        catalog=catalog,
        schema=schema,
        timestamp=timestamp,
    )
    apx.mkdir(parents=True, exist_ok=True)
    (apx / "schema.json").write_text(dump_schema_cache(manifest))
    (apx / "topology_metadata.json").write_text(json.dumps(
        {
            "source": "databricks-industry-solutions/lakehouse-industry-data-models",
            "node_metadata": industry_model_topology_metadata(
                model_json,
                catalog=catalog,
                schema=schema,
            ),
        },
        indent=2,
        sort_keys=True,
    ) + "\n")
    return manifest
