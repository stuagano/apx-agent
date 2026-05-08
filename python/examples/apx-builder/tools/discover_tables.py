from apx_agent import Dependencies


def search_tables(search_term: str, sql: Dependencies.Sql) -> list[dict]:
    """Search Unity Catalog for tables matching a term. Returns catalog.schema.table identifiers."""
    rows = sql(
        "SELECT table_catalog, table_schema, table_name, comment "
        "FROM system.information_schema.tables "
        "WHERE lower(table_name) LIKE :pattern "
        "   OR lower(coalesce(comment, '')) LIKE :pattern "
        "LIMIT 20",
        parameters=[{"name": "pattern", "value": f"%{search_term.lower()}%", "type": "STRING"}],
    )
    return [
        {
            "identifier": f"{r['table_catalog']}.{r['table_schema']}.{r['table_name']}",
            "comment": r.get("comment") or "",
        }
        for r in rows
    ]


def list_genie_spaces(ws: Dependencies.UserClient) -> list[dict]:
    """List Genie spaces available in this workspace. Returns id and name for each space."""
    response = ws.api_client.do("GET", "/api/2.0/genie/spaces")
    return [
        {"id": s["space_id"], "name": s["title"]}
        for s in response.get("spaces", [])
    ]
