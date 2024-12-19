import matplotlib.pyplot as plt  # noqa: D100
import networkx as nx

# Define the call graph as a directed graph
call_graph = nx.DiGraph()

# Add nodes and edges based on the extracted methods and their calls
call_graph.add_edges_from(
    [
        ("BackupManager.__init__", "BaseBackupManager.__init__"),
        ("BackupManager.__init__", "Path(hass.config.path)"),
        ("BackupManager.load_backups", "BackupManager._read_backups"),
        ("BackupManager._read_backups", "tarfile.open"),
        ("BackupManager._read_backups", "json_loads_object"),
        ("BackupManager._read_backups", "Backup.__init__"),
        ("BackupManager.async_get_backups", "BackupManager.load_backups"),
        ("BackupManager.async_get_backup", "BackupManager.load_backups"),
        ("BackupManager.async_remove_backup", "BackupManager.async_get_backup"),
        ("BackupManager.async_remove_backup", "backup.path.unlink"),
        ("BackupManager.async_create_backup", "BackupManager.async_pre_backup_actions"),
        (
            "BackupManager.async_create_backup",
            "BackupManager._mkdir_and_generate_backup_contents",
        ),
        (
            "BackupManager.async_create_backup",
            "BackupManager.async_post_backup_actions",
        ),
        ("BackupManager.async_create_backup", "_generate_slug"),
        ("BackupManager.async_create_backup", "Backup.__init__"),
        ("BackupManager._mkdir_and_generate_backup_contents", "SecureTarFile"),
        ("BackupManager._mkdir_and_generate_backup_contents", "json_bytes"),
        ("BackupManager._mkdir_and_generate_backup_contents", "tarfile.TarInfo"),
        ("BackupManager._mkdir_and_generate_backup_contents", "atomic_contents_add"),
        ("BackupManager.async_restore_backup", "BackupManager.async_get_backup"),
        ("BackupManager.async_restore_backup", "Path.write_text"),
        ("BackupManager.async_restore_backup", "HomeAssistant.services.async_call"),
        ("_generate_slug", "hashlib.sha1"),
    ]
)

# Draw the graph
plt.figure(figsize=(16, 12))
pos = nx.spring_layout(call_graph, k=0.5, seed=42)  # Layout for better spacing
nx.draw_networkx_nodes(call_graph, pos, node_size=2000, node_color="skyblue")
nx.draw_networkx_edges(
    call_graph, pos, arrowstyle="->", arrowsize=20, edge_color="gray"
)
nx.draw_networkx_labels(
    call_graph, pos, font_size=10, font_color="black", font_weight="bold"
)

plt.title("Call Graph for BackupManager (manager.py)", fontsize=16)
plt.axis("off")
plt.show()
