from datetime import date

from gerry.snapshots import SnapshotStore


def test_snapshot_store_lists_persisted_snapshots(tmp_path):
    store = SnapshotStore(tmp_path)
    first = store.create("election-a", date(2023, 10, 15))
    second = store.create("election-b", date(2024, 4, 7))

    listed = store.list()

    assert {snapshot.id for snapshot in listed} == {first.id, second.id}
    assert {snapshot.election_id for snapshot in listed} == {
        "election-a", "election-b"
    }
