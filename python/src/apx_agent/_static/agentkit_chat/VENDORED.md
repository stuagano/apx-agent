# Vendored: AgentKit chat app

Source: `databricks-agentbricks==0.5.0`, `databricks_agentbricks/templates/ui/agent-langgraph/ui/`
(the LangGraph variant, because apx's durable handler is LangGraph).
The server side is ported in `apx_agent/_ui_agentkit_chat.py` from `runtime/ui.py:install_ui`.

Path rewrites, applied to `index.html`, `app.js` and `styles.css`:

- `/ui-assets/` → `/_apx/assets/agentkit/`
- `/api/demo/` → `/_apx/api/demo/`
- `/api/ui/` → `/_apx/api/ui/`
- `/api/invocations` is unchanged (DurableAgentServer owns it).

To re-sync, copy the same directory from the new package version and run:

```bash
sed -i '' -e 's#/ui-assets/#/_apx/assets/agentkit/#g' \
  -e 's#"/api/demo/#"/_apx/api/demo/#g' -e 's#`/api/demo/#`/_apx/api/demo/#g' \
  -e 's#"/api/ui/#"/_apx/api/ui/#g' index.html app.js styles.css
```

Then run `tests/test_agentkit_chat.py`. It fails on any AgentKit path left unrewritten.
