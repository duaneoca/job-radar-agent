"""
Graph assembly — wires the V2 nodes into a LangGraph StateGraph that processes ONE email.

    screen ─┬─ finalize                       (sender rejected → Unprocessed)
            ├─ no_postings → finalize         (no usable links)
            └─ pick → verify ─┬─ write → finalize
                              ├─ prepare_retry → pick      (up to MAX_ATTEMPTS picks)
                              └─ escalate → finalize        (→ Unprocessed)
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from .nodes import Nodes
from .state import AgentState


def build_graph(nodes: Nodes):
    g = StateGraph(AgentState)

    g.add_node("screen", nodes.screen)
    g.add_node("pick", nodes.pick)
    g.add_node("verify", nodes.verify)
    g.add_node("prepare_retry", nodes.prepare_retry)
    g.add_node("write", nodes.write)
    g.add_node("no_postings", nodes.no_postings)
    g.add_node("escalate", nodes.escalate)
    g.add_node("finalize", nodes.finalize)

    g.set_entry_point("screen")
    g.add_conditional_edges("screen", nodes.after_screen,
                            {"pick": "pick", "no_postings": "no_postings", "finalize": "finalize"})
    g.add_edge("pick", "verify")
    g.add_conditional_edges("verify", nodes.gate,
                            {"write": "write", "retry": "prepare_retry", "escalate": "escalate"})
    g.add_edge("prepare_retry", "pick")
    for terminal in ("write", "no_postings", "escalate"):
        g.add_edge(terminal, "finalize")
    g.add_edge("finalize", END)
    return g.compile()
