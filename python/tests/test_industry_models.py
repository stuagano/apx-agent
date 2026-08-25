from __future__ import annotations

import json


def _model_json(path):
    data = {
        "model": {
            "name": "Banking",
            "description": "Retail banking model.",
            "industry_alignment": "banking",
            "core_business_processes": "KYC, account servicing, payments.",
            "data_domains": "Customer, Account",
            "common_business_jargons": "KYC (Know Your Customer), DDA.",
            "domains": [
                {
                    "name": "customer",
                    "products": [
                        {
                            "name": "party",
                            "table_name": "party",
                            "primary_key": "party_id",
                            "subdomain": "customer_identity",
                            "type": "Master",
                            "data_type": "master_data",
                            "description": "Golden master party record.",
                            "attributes": [
                                {
                                    "column_name": "party_id",
                                    "type": "BIGINT",
                                    "business_glossary_term": "Party Identifier",
                                    "description": "Unique party id.",
                                    "tags": "primary_key",
                                },
                                {
                                    "column_name": "country_id",
                                    "type": "BIGINT",
                                    "description": "Domicile country.",
                                    "foreign_key_to": "reference.country.country_id",
                                },
                            ],
                        }
                    ],
                },
                {
                    "name": "account",
                    "products": [
                        {
                            "name": "party",
                            "table_name": "party",
                            "description": "Duplicate source table name in another domain.",
                            "attributes": [
                                {"column_name": "party_id", "type": "BIGINT"}
                            ],
                        }
                    ],
                },
            ],
            "metric_views": [
                {
                    "view_name": "party_quality",
                    "owner_product": "party",
                    "description": "Party quality metrics.",
                }
            ],
        }
    }
    path.write_text(json.dumps(data))
    return path


def test_industry_model_manifest_preserves_columns_and_dedupes(tmp_path):
    from apx_agent._industry_models import industry_model_manifest

    manifest = industry_model_manifest(
        _model_json(tmp_path / "model.json"),
        catalog="main",
        schema="banking",
    )

    assert manifest == {
        "catalog": "main",
        "schema": "banking",
        "tables": {
            "party": ["party_id(BIGINT)", "country_id(BIGINT)"],
            "account__party": ["party_id(BIGINT)"],
        },
    }


def test_industry_model_okf_bundle_contains_ontology_grounding(tmp_path):
    from apx_agent._industry_models import write_industry_model_okf_bundle
    from apx_agent._okf import OKFDocument, okf_grounding, okf_glossary, okf_manifest

    okf = tmp_path / "okf"
    manifest = write_industry_model_okf_bundle(
        _model_json(tmp_path / "model.json"),
        okf,
        catalog="main",
        schema="banking",
        timestamp="2026-08-25T00:00:00+00:00",
    )

    assert okf_manifest(okf) == manifest
    party = OKFDocument.parse((okf / "tables" / "party.md").read_text())
    assert "Golden master party record." in party.body
    assert "* Domain: `customer`" in party.body
    assert "`country_id` -> `reference.country.country_id`" in party.body
    assert "term: Party Identifier" in party.body

    dataset = (okf / "datasets" / "banking.md").read_text()
    assert "party_quality" in dataset
    assert "KYC, account servicing, payments." in dataset
    assert okf_grounding(okf)["party"]["description"] == "Golden master party record."
    assert okf_glossary(okf)[0]["term"] == "Party Identifier"


def test_ontology_jumpstart_cli_writes_apx_bundle(tmp_path):
    from click.testing import CliRunner

    from apx_agent.cli import agents
    from apx_agent._schema import load_baked_schema

    model_json = _model_json(tmp_path / "model.json")
    out = tmp_path / ".apx"
    result = CliRunner().invoke(
        agents,
        [
            "ontology-jumpstart",
            str(model_json),
            "--catalog",
            "main",
            "--schema",
            "banking",
            "--output",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (out / "okf" / "tables" / "party.md").is_file()
    assert (out / "schema.json").is_file()
    assert (out / "topology_metadata.json").is_file()
    assert load_baked_schema(tmp_path)["tables"]["party"] == [
        "party_id(BIGINT)",
        "country_id(BIGINT)",
    ]
    from apx_agent._dev import _load_topology_node_metadata

    metadata = _load_topology_node_metadata(tmp_path)
    assert metadata["uc:main.banking.party"]["question_answer_pairs"][0]["question"] == (
        "What does party tell us in the customer model?"
    )


def test_ontology_jumpstart_force_removes_stale_okf_tables(tmp_path):
    from click.testing import CliRunner

    from apx_agent.cli import agents

    model_json = _model_json(tmp_path / "model.json")
    out = tmp_path / ".apx"
    stale = out / "okf" / "tables" / "stale.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old table")

    result = CliRunner().invoke(
        agents,
        [
            "ontology-jumpstart",
            str(model_json),
            "--catalog",
            "main",
            "--schema",
            "banking",
            "--output",
            str(out),
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not stale.exists()
    assert (out / "okf" / "tables" / "party.md").is_file()


def test_industry_model_question_pairs_feed_topology_metadata(tmp_path):
    from apx_agent import annotate_topology
    from apx_agent._apx_models import TopologyResponse
    from apx_agent._industry_models import (
        industry_model_question_pairs,
        industry_model_topology_metadata,
    )

    model_json = _model_json(tmp_path / "model.json")
    pairs = industry_model_question_pairs(
        model_json,
        catalog="main",
        schema="banking",
    )

    assert pairs[0]["question"] == "What does party tell us in the customer model?"
    assert pairs[0]["table"] == "main.banking.party"
    assert "Golden master party record" in pairs[0]["answer"]

    topology = {
        "rootId": "agent:root",
        "agentName": "banking-agent",
        "nodes": [
            {"id": "agent:root", "type": "DataAgent", "label": "banking-agent"},
            {"id": "uc:main.banking.party", "type": "UCFunction", "label": "main.banking.party"},
        ],
        "edges": [
            {
                "id": "agent:root->uc:main.banking.party:delegates-to",
                "source": "agent:root",
                "target": "uc:main.banking.party",
                "kind": "delegates-to",
            }
        ],
    }
    annotated = annotate_topology(
        topology,
        node_metadata=industry_model_topology_metadata(
            model_json,
            catalog="main",
            schema="banking",
        ),
    )

    table_node = annotated["nodes"][1]
    assert table_node["metadata"]["domain"] == "customer"
    assert table_node["metadata"]["question_answer_pairs"][0] == {
        "question": "What does party tell us in the customer model?",
        "answer": (
            "Golden master party record Primary key: `party_id`. "
            "1 declared foreign key relationship."
        ),
        "source": "databricks-industry-solutions/lakehouse-industry-data-models",
    }
    response = TopologyResponse.model_validate(annotated)
    assert response.nodes[1].model_extra["metadata"]["question_answer_pairs"]
