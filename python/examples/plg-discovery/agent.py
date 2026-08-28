"""Native APX declaration for the PLG discovery AppKit host."""

from apx_agent import Agent

from server.grounding import build_system_prompt
from server.tools import active_tools, load_default_skills


load_default_skills()

agent = Agent(
    name="discovery",
    description="Discover a nonprofit's current systems and recommend a governed technology blueprint.",
    instructions=build_system_prompt(),
    tools=active_tools(),
)
