from strategy_lab.private_storage import initialize_private_storage


def test_initialize_private_storage_creates_expected_layout(tmp_path) -> None:
    layout = initialize_private_storage(tmp_path)

    assert (tmp_path / "README_PRIVATE_STORAGE.md").exists()
    assert (tmp_path / "data" / "market_data").is_dir()
    assert (tmp_path / "data" / "experiment_logs").is_dir()
    assert (tmp_path / "inbox" / "public_worker_results").is_dir()
    assert len(layout.directories) >= 5

