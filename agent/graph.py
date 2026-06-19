"""
Graph assembly — wires the nodes into a LangGraph StateGraph for processing ONE email.

    classify → critic → gate ─┬─ retry → prepare_retry → classify
                              ├─ route → route_by_category ─┬─ extract_recruiter → write_postings ─┐
                              │                              ├─ write_postings ───────────────────┤
                              │                              ├─ write_interaction ────────────────┤→ finalize → END
                              │                              └─ social_discard ───────────────────┘
                              └─ escalate ───────────────────────────────────────────────────────┘

    (extract_recruiter runs only for recruiter_outreach — a best-effort recruiter card, then the
     standard posting write.)

A checkpointer (SQLite local / Postgres cloud) is attached by the caller to enable HITL
pause/resume; tests compile without one.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from .nodes import Nodes
from .state import AgentState


def build_graph(nodes: Nodes, checkpointer=None):
    g = StateGraph(AgentState)

    g.add_node("classify", nodes.classify)
    g.add_node("critic", nodes.critic)
    g.add_node("prepare_retry", nodes.prepare_retry)
    g.add_node("write_postings", nodes.write_postings)
    g.add_node("extract_recruiter", nodes.extract_recruiter)
    g.add_node("write_interaction", nodes.write_interaction)
    g.add_node("social_discard", nodes.social_discard)
    g.add_node("escalate", nodes.escalate)
    g.add_node("finalize", nodes.finalize)

    g.set_entry_point("classify")
    g.add_edge("classify", "critic")

    g.add_conditional_edges(
        "critic",
        nodes.gate,
        {"route": "_route", "retry": "prepare_retry", "escalate": "escalate"},
    )
    # gate's "route" is a virtual decision; dispatch by category from a no-op router.
    g.add_node("_route", lambda state: {})
    g.add_conditional_edges(
        "_route",
        nodes.route_by_category,
        {
            "write_postings": "write_postings",
            "extract_recruiter": "extract_recruiter",
            "write_interaction": "write_interaction",
            "social_discard": "social_discard",
        },
    )
    g.add_edge("extract_recruiter", "write_postings")  # recruiter card → then the standard write

    g.add_edge("prepare_retry", "classify")
    for terminal in ("write_postings", "write_interaction", "social_discard", "escalate"):
        g.add_edge(terminal, "finalize")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer) if checkpointer else g.compile()
