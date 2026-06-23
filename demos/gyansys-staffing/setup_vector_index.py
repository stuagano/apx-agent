# demos/gyansys-staffing/setup_vector_index.py
"""Create a VS endpoint + delta-sync index over replicon_people.skill_profile."""
from __future__ import annotations

import time

from databricks.sdk import WorkspaceClient

# Catalog/schema/profile are workspace-specific (see load_to_uc.py).
PROFILE = "fevm-serverless-stable-qh44kx"
ENDPOINT = "gyansys_demo_vs"
SOURCE_TABLE = "serverless_stable_qh44kx_catalog.gyansys_staffing.replicon_people"
INDEX_NAME = "serverless_stable_qh44kx_catalog.gyansys_staffing.replicon_people_index"
EMBED_ENDPOINT = "databricks-gte-large-en"


def _ensure_endpoint(w: WorkspaceClient) -> None:
    from databricks.sdk.service.vectorsearch import EndpointType

    names = [e.name for e in w.vector_search_endpoints.list_endpoints()]
    if ENDPOINT not in names:
        w.vector_search_endpoints.create_endpoint(
            name=ENDPOINT, endpoint_type=EndpointType.STANDARD,
        )
    # Wait for ONLINE whether we just created it or it was already provisioning.
    w.vector_search_endpoints.wait_get_endpoint_vector_search_endpoint_online(
        endpoint_name=ENDPOINT,
    )
    print(f"endpoint {ENDPOINT} online")


def _ensure_index(w: WorkspaceClient) -> None:
    from databricks.sdk.service.vectorsearch import (
        DeltaSyncVectorIndexSpecRequest,
        EmbeddingSourceColumn,
        PipelineType,
        VectorIndexType,
    )

    existing = [i.name for i in w.vector_search_indexes.list_indexes(
        endpoint_name=ENDPOINT)]
    if INDEX_NAME in existing:
        print(f"index {INDEX_NAME} exists; syncing")
        w.vector_search_indexes.sync_index(index_name=INDEX_NAME)
        return

    w.vector_search_indexes.create_index(
        name=INDEX_NAME,
        endpoint_name=ENDPOINT,
        primary_key="person_id",
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=SOURCE_TABLE,
            pipeline_type=PipelineType.TRIGGERED,
            embedding_source_columns=[
                EmbeddingSourceColumn(
                    name="skill_profile",
                    embedding_model_endpoint_name=EMBED_ENDPOINT,
                ),
            ],
        ),
    )
    print(f"index {INDEX_NAME} created; waiting for first sync")
    # poll until the index reports ready
    for _ in range(60):
        idx = w.vector_search_indexes.get_index(index_name=INDEX_NAME)
        status = idx.status
        if status and status.ready:
            print("index ready")
            return
        time.sleep(15)
    raise RuntimeError("index did not become ready in time")


def main() -> None:
    w = WorkspaceClient(profile=PROFILE)
    _ensure_endpoint(w)
    _ensure_index(w)


if __name__ == "__main__":
    main()
