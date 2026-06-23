# GyanSys Staffing Coworker (demo)

Read-only Databricks demo: matches Salesforce opportunities to Replicon people
by skill similarity, surfaces stalled pipeline and staffing bandwidth.

Target workspace: `fe-stable`. UC location: `gyansys_demo.staffing`.

## Run order
1. `python generate_data.py` then `python load_to_uc.py`   (synthetic data → UC)
2. `python setup_vector_index.py`                            (people skill-profile VS index)
3. `uv run apx-agent agents run gyansys-staffing.yaml`       (local dev UI)
4. `uv run apx-agent agents deploy gyansys-staffing.yaml --target apps --profile fe-stable`

See ARCHITECTURE.md for the deep-dive talking points.
