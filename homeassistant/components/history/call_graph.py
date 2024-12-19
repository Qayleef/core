import matplotlib.pyplot as plt  # noqa: D100
import networkx as nx

# Define the relationships (edges) between functions in __init__.py
edges = [
    # From __init__.py
    ("async_setup", "HistoryPeriodView.get"),
    ("async_setup", "websocket_api.async_setup"),
    ("HistoryPeriodView.get", "dt_util.parse_datetime"),
    ("HistoryPeriodView.get", "valid_entity_id"),
    ("HistoryPeriodView.get", "has_recorder_run_after"),
    ("HistoryPeriodView.get", "entities_may_have_state_changes_after"),
    ("HistoryPeriodView.get", "get_instance.async_add_executor_job"),
    ("HistoryPeriodView.get", "_sorted_significant_states_json"),
    ("HistoryPeriodView._sorted_significant_states_json", "session_scope"),
    (
        "HistoryPeriodView._sorted_significant_states_json",
        "history.get_significant_states_with_session",
    ),
]

# Create a directed graph
graph = nx.DiGraph()
graph.add_edges_from(edges)

# Use spring layout with spacing adjustments
pos = nx.spring_layout(graph, seed=42, k=0.8)  # `k` controls the spacing

# Plot the graph
plt.figure(figsize=(10, 6))

# Draw the graph
nx.draw(
    graph,
    pos,
    with_labels=True,
    node_size=5000,
    node_color="orange",
    font_size=10,
    font_weight="bold",
    arrowsize=15,
)

# Add a title to the graph
plt.title("Call Graph for __init__.py", fontsize=14)

# Show the graph
plt.show()
