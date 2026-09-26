"""Tests for _data_governance.py — declared UC ABAC data governance.

Covers:
  1. DataConfig / DataTagConfig / DataTableConfig / DataPolicyConfig validation.
  2. compile_data_governance_plan emits correct DDL for tags, tables, policies.
  3. apply_data_governance executes DDL via run_sql (mocked ws).
  4. dry_run mode returns DDL without executing.
  5. Fail-closed: invalid config raises at parse time.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apx_agent._data_governance import (
    DataGovernancePlan,
    apply_data_governance,
    apply_default_agent_tags,
    compile_data_governance_plan,
    default_agent_tags_plan,
)
from apx_agent._models import (
    DataConfig,
    DataPolicyConfig,
    DataTableConfig,
    DataTagConfig,
)


# ===========================================================================
# Validation — fail-closed on bad input
# ===========================================================================


class TestDataTagConfig:
    def test_valid(self) -> None:
        tag = DataTagConfig(name="apx.agent.owner", values=["alice", "bob"])
        assert tag.name == "apx.agent.owner"
        assert tag.values == ["alice", "bob"]

    def test_free_form(self) -> None:
        tag = DataTagConfig(name="apx.agent.env")
        assert tag.values is None

    def test_empty_name_fails(self) -> None:
        with pytest.raises(ValueError, match="name must be non-empty"):
            DataTagConfig(name="")

    def test_empty_value_fails(self) -> None:
        with pytest.raises(ValueError, match="empty value"):
            DataTagConfig(name="x", values=["ok", ""])

    def test_extra_key_fails(self) -> None:
        with pytest.raises(ValueError):
            DataTagConfig(name="x", unknown_key="y")


class TestDataTableConfig:
    def test_valid(self) -> None:
        t = DataTableConfig(
            name="cat.sch.tbl",
            tags={"apx.agent.owner": "alice"},
        )
        assert t.name == "cat.sch.tbl"
        assert t.tags == {"apx.agent.owner": "alice"}
        assert t.policies == []

    def test_empty_name_fails(self) -> None:
        with pytest.raises(ValueError, match="name must be non-empty"):
            DataTableConfig(name="", tags={"k": "v"})

    def test_not_three_part_fails(self) -> None:
        with pytest.raises(ValueError, match="three-part UC name"):
            DataTableConfig(name="cat.tbl", tags={"k": "v"})

    def test_empty_part_fails(self) -> None:
        with pytest.raises(ValueError, match="three-part UC name"):
            DataTableConfig(name="cat..tbl", tags={"k": "v"})

    def test_no_tags_fails(self) -> None:
        with pytest.raises(ValueError, match="at least one tag"):
            DataTableConfig(name="cat.sch.tbl", tags={})

    def test_empty_tag_key_fails(self) -> None:
        with pytest.raises(ValueError, match="empty tag key"):
            DataTableConfig(name="cat.sch.tbl", tags={"": "v"})

    def test_empty_tag_value_fails(self) -> None:
        with pytest.raises(ValueError, match="empty tag key or value"):
            DataTableConfig(name="cat.sch.tbl", tags={"k": ""})


class TestDataPolicyConfig:
    def test_valid_row_filter(self) -> None:
        p = DataPolicyConfig(
            name="rls_region",
            type="row_filter",
            function="region = current_user()",
            to="analysts",
        )
        assert p.type == "row_filter"

    def test_valid_column_mask(self) -> None:
        p = DataPolicyConfig(
            name="mask_ssn",
            type="column_mask",
            function="'***-**-' || right(ssn, 4)",
            to="analysts",
            using_columns=["ssn"],
        )
        assert p.type == "column_mask"

    def test_column_mask_requires_match_or_using(self) -> None:
        with pytest.raises(ValueError, match="match_columns or using_columns"):
            DataPolicyConfig(
                name="bad",
                type="column_mask",
                function="f()",
                to="g",
            )

    def test_empty_name_fails(self) -> None:
        with pytest.raises(ValueError, match="name must be non-empty"):
            DataPolicyConfig(
                name="", type="row_filter", function="f", to="g"
            )

    def test_empty_function_fails(self) -> None:
        with pytest.raises(ValueError, match="function must be non-empty"):
            DataPolicyConfig(
                name="p", type="row_filter", function="", to="g"
            )

    def test_empty_to_fails(self) -> None:
        with pytest.raises(ValueError, match="to must be non-empty"):
            DataPolicyConfig(
                name="p", type="row_filter", function="f", to=""
            )


class TestDataConfig:
    def test_empty(self) -> None:
        cfg = DataConfig()
        assert cfg.governed_tags == []
        assert cfg.tables == []

    def test_full(self) -> None:
        cfg = DataConfig(
            governed_tags=[DataTagConfig(name="env", values=["dev", "prod"])],
            tables=[
                DataTableConfig(
                    name="cat.sch.tbl",
                    tags={"env": "dev"},
                    policies=[
                        DataPolicyConfig(
                            name="rls",
                            type="row_filter",
                            function="1=1",
                            to="all",
                        )
                    ],
                )
            ],
        )
        assert len(cfg.governed_tags) == 1
        assert len(cfg.tables) == 1
        assert len(cfg.tables[0].policies) == 1


# ===========================================================================
# Plan compilation — pure function, no workspace
# ===========================================================================


class TestCompilePlan:
    def test_empty_config(self) -> None:
        plan = compile_data_governance_plan(DataConfig())
        assert plan.all_ddl == []

    def test_governed_tag_with_values(self) -> None:
        cfg = DataConfig(
            governed_tags=[DataTagConfig(name="env", values=["dev", "prod"])]
        )
        plan = compile_data_governance_plan(cfg)
        assert len(plan.governed_tag_ddl) == 1
        assert "CREATE GOVERNED TAG IF NOT EXISTS `env`" in plan.governed_tag_ddl[0]
        assert "'dev'" in plan.governed_tag_ddl[0]
        assert "'prod'" in plan.governed_tag_ddl[0]

    def test_governed_tag_free_form(self) -> None:
        cfg = DataConfig(governed_tags=[DataTagConfig(name="owner")])
        plan = compile_data_governance_plan(cfg)
        assert "VALUES" not in plan.governed_tag_ddl[0]

    def test_table_tags(self) -> None:
        cfg = DataConfig(
            tables=[
                DataTableConfig(
                    name="cat.sch.tbl",
                    tags={"env": "dev", "owner": "alice"},
                )
            ]
        )
        plan = compile_data_governance_plan(cfg)
        assert len(plan.table_tag_ddl) == 1
        assert "ALTER TABLE `cat`.`sch`.`tbl` SET TAGS" in plan.table_tag_ddl[0]
        assert "'env' = 'dev'" in plan.table_tag_ddl[0]
        assert "'owner' = 'alice'" in plan.table_tag_ddl[0]

    def test_row_filter_policy(self) -> None:
        cfg = DataConfig(
            tables=[
                DataTableConfig(
                    name="cat.sch.tbl",
                    tags={"env": "dev"},
                    policies=[
                        DataPolicyConfig(
                            name="rls",
                            type="row_filter",
                            function="region = current_user()",
                            to="analysts",
                            when="env = 'prod'",
                        )
                    ],
                )
            ]
        )
        plan = compile_data_governance_plan(cfg)
        assert len(plan.policy_ddl) == 1
        ddl = plan.policy_ddl[0]
        assert "CREATE OR REPLACE POLICY `rls`" in ddl
        assert "ON TABLE `cat`.`sch`.`tbl`" in ddl
        assert "ROW FILTER" in ddl
        assert "region = current_user()" in ddl
        assert "TO `analysts`" in ddl
        assert "WHEN env = 'prod'" in ddl

    def test_column_mask_policy(self) -> None:
        cfg = DataConfig(
            tables=[
                DataTableConfig(
                    name="cat.sch.tbl",
                    tags={"env": "dev"},
                    policies=[
                        DataPolicyConfig(
                            name="mask_ssn",
                            type="column_mask",
                            function="'***'",
                            to="analysts",
                            match_columns={"pii": "ssn"},
                            using_columns=["ssn"],
                        )
                    ],
                )
            ]
        )
        plan = compile_data_governance_plan(cfg)
        ddl = plan.policy_ddl[0]
        assert "COLUMN MASK" in ddl
        assert "MATCH COLUMNS has_tag_value('pii', 'ssn')" in ddl
        assert "USING COLUMNS (`ssn`)" in ddl

    def test_execution_order(self) -> None:
        cfg = DataConfig(
            governed_tags=[DataTagConfig(name="env")],
            tables=[
                DataTableConfig(
                    name="cat.sch.tbl",
                    tags={"env": "dev"},
                    policies=[
                        DataPolicyConfig(
                            name="rls",
                            type="row_filter",
                            function="1=1",
                            to="all",
                        )
                    ],
                )
            ],
        )
        plan = compile_data_governance_plan(cfg)
        all_ddl = plan.all_ddl
        assert len(all_ddl) == 3
        assert "GOVERNED TAG" in all_ddl[0]
        assert "ALTER TABLE" in all_ddl[1]
        assert "CREATE OR REPLACE POLICY" in all_ddl[2]

    def test_sql_injection_escaped(self) -> None:
        cfg = DataConfig(
            governed_tags=[DataTagConfig(name="env", values=["it's"])]
        )
        plan = compile_data_governance_plan(cfg)
        assert "\\'" in plan.governed_tag_ddl[0]


# ===========================================================================
# Apply — mocked workspace
# ===========================================================================


class TestApply:
    def test_dry_run_no_execution(self) -> None:
        plan = DataGovernancePlan(
            governed_tag_ddl=["CREATE GOVERNED TAG IF NOT EXISTS `env`"],
        )
        ws = MagicMock()
        executed = apply_data_governance(plan, ws, dry_run=True)
        assert executed == ["CREATE GOVERNED TAG IF NOT EXISTS `env`"]
        ws.statement_execution.execute_statement.assert_not_called()

    def test_apply_executes(self) -> None:
        plan = DataGovernancePlan(
            governed_tag_ddl=["CREATE GOVERNED TAG IF NOT EXISTS `env`"],
        )
        ws = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status.state = "SUCCEEDED"
        mock_resp.result = None
        ws.statement_execution.execute_statement.return_value = mock_resp
        with patch("apx_agent._data_governance.run_sql") as mock_run:
            mock_run.return_value = []
            executed = apply_data_governance(plan, ws, warehouse_id="wh-1")
        assert len(executed) == 1
        mock_run.assert_called_once_with(
            ws, "CREATE GOVERNED TAG IF NOT EXISTS `env`", warehouse_id="wh-1"
        )

    def test_apply_fails_closed(self) -> None:
        plan = DataGovernancePlan(
            governed_tag_ddl=["CREATE GOVERNED TAG IF NOT EXISTS `env`"],
        )
        ws = MagicMock()
        with patch("apx_agent._data_governance.run_sql") as mock_run:
            mock_run.side_effect = RuntimeError("DDL failed")
            with pytest.raises(RuntimeError, match="DDL failed"):
                apply_data_governance(plan, ws)

    def test_apply_empty_plan(self) -> None:
        plan = DataGovernancePlan()
        ws = MagicMock()
        executed = apply_data_governance(plan, ws)
        assert executed == []


# ===========================================================================
# Default apx.agent.* tags for auto-created agent tables (tag-at-auto_create)
# ===========================================================================


class TestDefaultAgentTags:
    def test_plan_compiles_managed_and_kind_tags(self) -> None:
        plan = default_agent_tags_plan("main.apx.agent_registry", kind="registry")
        ddl = plan.all_ddl
        assert len(ddl) == 3
        assert ddl[0] == (
            "CREATE GOVERNED TAG IF NOT EXISTS `apx.agent.managed` VALUES ('true')"
        )
        assert ddl[1].startswith("CREATE GOVERNED TAG IF NOT EXISTS `apx.agent.kind`")
        for kind in ("registry", "tools", "traces", "violations"):
            assert f"'{kind}'" in ddl[1]
        assert ddl[2] == (
            "ALTER TABLE `main`.`apx`.`agent_registry` SET TAGS "
            "('apx.agent.managed' = 'true', 'apx.agent.kind' = 'registry')"
        )

    def test_plan_kind_value_matches_table(self) -> None:
        plan = default_agent_tags_plan("main.x.violations", kind="violations")
        assert "'apx.agent.kind' = 'violations'" in plan.table_tag_ddl[0]

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown agent table kind"):
            default_agent_tags_plan("c.s.t", kind="memory")

    def test_apply_success_executes_all_ddl(self) -> None:
        ws = MagicMock()
        with patch("apx_agent._data_governance.run_sql") as mock_run:
            assert apply_default_agent_tags(
                "main.x.traces", kind="traces", ws=ws, warehouse_id="wh-1"
            ) is True
        stmts = [c.args[1] for c in mock_run.call_args_list]
        assert len(stmts) == 3
        assert all(c.kwargs["warehouse_id"] == "wh-1" for c in mock_run.call_args_list)
        assert "GOVERNED TAG" in stmts[0] and "SET TAGS" in stmts[2]

    def test_apply_failure_warns_and_returns_false(self, caplog) -> None:
        ws = MagicMock()
        with (
            patch(
                "apx_agent._data_governance.run_sql",
                side_effect=RuntimeError("governed tags not supported"),
            ),
            caplog.at_level("WARNING", logger="apx_agent._data_governance"),
        ):
            assert apply_default_agent_tags("c.s.t", kind="violations", ws=ws) is False
        assert "untagged" in caplog.text
        assert "governed tags not supported" in caplog.text
