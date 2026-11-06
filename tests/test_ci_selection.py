"""分组覆盖、分片无遗漏及稳定性，防止 CI 优化悄悄减少覆盖。"""
from ci_selection import split_nodes, suite_for


def test_every_offline_node_has_exactly_one_suite():
    examples = [
        ("tests/test_new_feature.py::test_new", set(), [], "fast"),
        ("tests/test_pipeline.py::test_prepare", set(), ["video_file"], "media"),
        ("tests/test_agent_panel_ui.py::test_panel", {"slow"}, [], "media"),
        ("tests/test_backup.py::test_roundtrip", {"slow"}, ["video_file"], "backup"),
        ("tests/test_platform_management.py::test_backup_full", set(), [], "backup"),
    ]
    for nodeid, markers, fixtures, expected in examples:
        assert suite_for(nodeid, markers, fixtures) == expected
    assert suite_for("tests/test_pg_backend.py::test_roundtrip", {"requires_postgres"}, []) is None
    assert suite_for("tests/test_s3_store.py::test_read", {"requires_s3"}, []) is None
    assert suite_for("tests/test_chroma_server.py::test_read", {"requires_chroma_server"}, []) is None


def test_shards_partition_all_nodes_without_duplicates_and_ignore_input_order():
    nodes = [f"tests/test_{i % 3}.py::test_case_{i}" for i in range(60)]
    costs = {"test_0.py": 10.0}
    groups = split_nodes(nodes, 4, costs)
    flattened = [node for group in groups for node in group]
    assert len(flattened) == len(set(flattened)) == len(nodes)
    assert set(flattened) == set(nodes)
    assert all(groups)
    assert groups == split_nodes(list(reversed(nodes)), 4, costs)


def test_new_tests_are_not_lost_when_no_timing_exists():
    nodes = ["tests/test_new.py::test_new", "tests/test_old.py::test_old"]
    groups = split_nodes(nodes, 2, {"test_old.py": 50.0})
    assert all(len(group) == 1 for group in groups)
    assert {node for group in groups for node in group} == set(nodes)
