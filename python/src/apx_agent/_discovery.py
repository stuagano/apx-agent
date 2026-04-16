"""Auto-discovery — deprecated.

The APX-template auto-import convention is no longer needed. Consumers
should use the explicit ``create_app(agent)`` pattern instead::

    from apx_agent import Agent, create_app

    agent = Agent(tools=[my_tool])
    app = create_app(agent)
"""
