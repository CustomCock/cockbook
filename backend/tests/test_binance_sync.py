"""Sync-Prozedur nach Binance-Doku – jede Regel mit eigenem Testfall.

Konvention in den Fixtures: Preise in Ticks (Tick-Größe 1), Update-IDs mit Lücken,
wie bei Binance Futures üblich (IDs sind nicht fortlaufend, deshalb gibt es ``pu``).
"""

from decimal import Decimal

import pytest

from cockbook.adapters.binance import (
    BinanceDepthSync,
    DepthSnapshot,
    DepthUpdate,
    StaleSnapshotError,
)
from cockbook.core.events import BookDelta, BookSnapshot, BookSyncStatus, Event, PriceLevel, Side
from cockbook.core.instrument import Instrument

INST = Instrument("binance_usdm", "TEST", Decimal(1), Decimal("0.001"))

Lv = tuple[tuple[int, str], ...]


def lv(levels: Lv) -> tuple[PriceLevel, ...]:
    return tuple(PriceLevel(p, Decimal(q)) for p, q in levels)


def upd(first: int, final: int, prev: int, bids: Lv = (), asks: Lv = ()) -> DepthUpdate:
    return DepthUpdate(first, final, prev, 0, lv(bids), lv(asks), recv_ns=final)


def snap(
    last_id: int, bids: Lv = ((100, "1"), (99, "2")), asks: Lv = ((101, "1"),)
) -> DepthSnapshot:
    return DepthSnapshot(last_id, None, lv(bids), lv(asks), recv_ns=last_id)


def new_sync(**kw: object) -> BinanceDepthSync:
    kw.setdefault("coverage_refresh_fraction", 1.0)
    return BinanceDepthSync(INST, **kw)  # type: ignore[arg-type]


def types(events: list[Event]) -> list[str]:
    out = []
    for e in events:
        if isinstance(e, BookSyncStatus):
            out.append(f"status:{e.synced}")
        else:
            out.append(type(e).__name__)
    return out


# --- Schritt 1-4: Puffern, Snapshot, alte Events verwerfen, Brücken-Event -----------------


def test_buffers_until_snapshot() -> None:
    s = new_sync()
    assert s.on_depth(upd(10, 20, 5)) == []
    assert not s.synced
    assert s.wants_snapshot


def test_no_snapshot_wanted_without_buffered_event() -> None:
    assert not new_sync().wants_snapshot


def test_snapshot_drops_old_and_applies_bridging_event() -> None:
    s = new_sync()
    s.on_depth(upd(10, 20, 5, bids=((100, "9"),)))  # u=20 < 25 -> verwerfen
    s.on_depth(upd(21, 30, 20, bids=((100, "5"),)))  # U=21 <= 25 <= u=30 -> Brücke
    s.on_depth(upd(31, 40, 30, asks=((101, "0"), (102, "3"))))
    events = s.on_snapshot(snap(25))

    assert types(events) == ["BookSnapshot", "status:True", "BookDelta", "BookDelta"]
    assert isinstance(events[0], BookSnapshot)
    assert events[0].last_seq == 25
    deltas = [e for e in events if isinstance(e, BookDelta)]
    assert [d.last_seq for d in deltas] == [30, 40]
    assert s.synced
    assert s.last_update_id == 40
    assert s.book.qty(Side.BID, 100) == Decimal(5)
    assert s.book.best_ask() == PriceLevel(102, Decimal(3))


def test_event_with_u_equal_last_update_id_is_bridging() -> None:
    s = new_sync()
    s.on_depth(upd(20, 25, 19))
    events = s.on_snapshot(snap(25))
    assert types(events) == ["BookSnapshot", "status:True", "BookDelta"]


def test_snapshot_older_than_buffer_raises_and_keeps_buffer() -> None:
    s = new_sync()
    s.on_depth(upd(50, 60, 40))
    with pytest.raises(StaleSnapshotError):
        s.on_snapshot(snap(45))  # U=50 > 45: Updates 46..49 fehlen
    assert not s.synced
    assert s.wants_snapshot
    assert s.stats.stale_snapshots == 1
    # Neuer, passender Snapshot funktioniert danach
    assert types(s.on_snapshot(snap(55))) == ["BookSnapshot", "status:True", "BookDelta"]


def test_snapshot_newer_than_buffer_waits_for_bridging_event() -> None:
    s = new_sync()
    s.on_depth(upd(10, 20, 5))
    events = s.on_snapshot(snap(25))  # alles im Puffer zu alt
    assert types(events) == ["BookSnapshot", "status:True"]
    assert s.on_depth(upd(15, 22, 20)) == []  # u=22 < 25 -> verwerfen
    assert types(s.on_depth(upd(23, 27, 22))) == ["BookDelta"]  # Brücke
    assert types(s.on_depth(upd(28, 31, 27))) == ["BookDelta"]


def test_first_event_after_snapshot_with_gap_triggers_resync() -> None:
    s = new_sync()
    s.on_depth(upd(10, 20, 5))
    s.on_snapshot(snap(25))
    events = s.on_depth(upd(30, 35, 28))  # U=30 > 25 -> Snapshot zu alt
    assert types(events) == ["status:False"]
    assert not s.synced
    assert s.wants_snapshot  # Event liegt wieder im Puffer
    assert s.stats.stale_snapshots == 1


# --- Schritt 5: pu-Kette ------------------------------------------------------------------


def synced_at(last: int = 30) -> BinanceDepthSync:
    s = new_sync()
    s.on_depth(upd(last - 5, last, last - 6))
    s.on_snapshot(snap(last - 2))
    assert s.synced
    assert s.last_update_id == last
    return s


def test_contiguous_pu_chain_applies() -> None:
    s = synced_at(30)
    assert types(s.on_depth(upd(33, 40, 30))) == ["BookDelta"]
    assert types(s.on_depth(upd(41, 41, 40))) == ["BookDelta"]
    assert s.stats.gaps == 0


def test_gap_resets_and_rebuffers() -> None:
    s = synced_at(30)
    events = s.on_depth(upd(40, 50, 35))  # pu=35 != 30
    assert types(events) == ["status:False"]
    assert isinstance(events[0], BookSyncStatus)
    assert "gap" in events[0].reason
    assert s.stats.gaps == 1
    assert not s.synced
    assert s.book.depth == (0, 0)  # nie still mit kaputtem Buch weiterlaufen
    assert s.wants_snapshot
    # Neu-Sync mit frischem Snapshot
    assert types(s.on_snapshot(snap(45))) == ["BookSnapshot", "status:True", "BookDelta"]
    assert s.last_update_id == 50


def test_duplicate_event_is_a_gap() -> None:
    s = synced_at(30)
    s.on_depth(upd(31, 40, 30))
    assert types(s.on_depth(upd(31, 40, 30))) == ["status:False"]


def test_gap_inside_buffer_replay_resyncs() -> None:
    s = new_sync()
    s.on_depth(upd(20, 30, 10))
    s.on_depth(upd(31, 40, 30))
    s.on_depth(upd(50, 60, 45))  # Lücke im Puffer
    events = s.on_snapshot(snap(25))
    assert types(events) == [
        "BookSnapshot",
        "status:True",
        "BookDelta",
        "BookDelta",
        "status:False",
    ]
    assert not s.synced
    assert s.wants_snapshot


def test_reset_on_disconnect() -> None:
    s = synced_at(30)
    status = s.reset("disconnected", 0)
    assert not status.synced
    assert not s.synced
    assert not s.wants_snapshot  # erst puffern nach Reconnect


def test_buffer_is_bounded() -> None:
    s = new_sync(max_buffer=3)
    for i in range(5):
        s.on_depth(upd(i * 10 + 1, i * 10 + 10, i * 10))
    assert s.stats.buffer_overflows == 2
    # Älteste sind weg -> Snapshot vor ihnen ist zu alt
    with pytest.raises(StaleSnapshotError):
        s.on_snapshot(snap(5))


def test_crossed_book_triggers_resync() -> None:
    s = synced_at(30)
    events = s.on_depth(upd(31, 40, 30, bids=((105, "1"),)))  # Bid über bestem Ask
    assert types(events) == ["BookDelta", "status:False"]
    assert s.stats.crossed == 1


def test_mid_leaving_coverage_refreshes() -> None:
    s = BinanceDepthSync(INST, coverage_refresh_fraction=0.5)
    s.on_depth(upd(1, 10, 0))
    s.on_snapshot(snap(5, bids=((100, "1"), (80, "1")), asks=((101, "1"), (121, "1"))))
    # Coverage 80..121, Mid 100.5 -> Grenzen 90.25 .. 110.75
    assert types(s.on_depth(upd(11, 20, 10, bids=((100, "0"), (95, "1"))))) == ["BookDelta"]
    events = s.on_depth(upd(21, 30, 20, bids=((95, "0"), (80, "0")), asks=((101, "0"),)))
    # Bids leer -> kein Mid berechenbar -> kein Refresh; jetzt Asks weit weg ziehen
    assert types(events) == ["BookDelta"]
    events = s.on_depth(upd(31, 40, 30, bids=((115, "1"),), asks=((121, "0"), (116, "1"))))
    assert types(events) == ["BookDelta", "status:False"]
    assert s.stats.coverage_refreshes == 1


# --- Validierung --------------------------------------------------------------------------


def test_validation_ok_when_books_match() -> None:
    s = synced_at(30)
    s.on_depth(upd(31, 40, 30, bids=((99, "7"),)))
    assert s.request_validation(snap(40, bids=((100, "1"), (99, "7")))) == []
    assert s.last_validation is not None
    assert s.last_validation.ok
    assert s.stats.validations_ok == 1


def test_validation_detects_mismatch_and_resyncs() -> None:
    s = synced_at(30)
    events = s.request_validation(snap(30, bids=((100, "1"), (99, "3"))))  # lokal 99 -> 2
    assert types(events) == ["status:False"]
    assert s.stats.validations_failed == 1
    assert s.last_validation is not None
    assert s.last_validation.mismatches == [("bid", 99, Decimal(2), Decimal(3))]


def test_validation_detects_ghost_and_missing_levels() -> None:
    s = new_sync()
    s.on_depth(upd(25, 30, 24))
    s.on_snapshot(snap(28, bids=((100, "1"), (99, "2"), (97, "1"))))  # Coverage ab 97
    # Börse: 99 existiert nicht (Ghost lokal), 98 fehlt lokal
    s.request_validation(snap(30, bids=((100, "1"), (98, "5"), (97, "1"))))
    assert s.last_validation is not None
    assert sorted(s.last_validation.mismatches) == [
        ("bid", 98, Decimal(0), Decimal(5)),
        ("bid", 99, Decimal(2), Decimal(0)),
    ]


def test_validation_ignores_levels_outside_sync_coverage() -> None:
    s = synced_at(30)  # Sync-Snapshot: Bids 100, 99 -> Coverage ab 99
    # 98 lag nie im Sync-Snapshot: lokal unbekannt, darf kein Mismatch sein
    s.request_validation(snap(30, bids=((100, "1"), (99, "2"), (98, "5"))))
    assert s.last_validation is not None
    assert s.last_validation.ok


def test_validation_ignores_levels_below_snapshot_range() -> None:
    s = synced_at(30)
    # Kontroll-Snapshot reicht nur bis 100 -> Level 99 liegt außerhalb und zählt nicht
    assert s.request_validation(snap(30, bids=((100, "1"),))) == []
    assert s.last_validation is not None
    assert s.last_validation.ok


def test_validation_excludes_levels_touched_after_snapshot_id() -> None:
    """Snapshot bei L=30, lokal schon bei 50: geänderte Preise werden ausgeschlossen."""
    s = synced_at(30)
    s.on_depth(upd(31, 40, 30, bids=((99, "8"),)))
    s.on_depth(upd(41, 50, 40, asks=((101, "4"),)))
    s.request_validation(snap(30))  # Börse bei 30: 99->2, 101->1
    assert s.last_validation is not None
    assert s.last_validation.ok
    assert s.last_validation.excluded_touched == 2


def test_validation_waits_until_local_book_reaches_snapshot_id() -> None:
    s = synced_at(30)
    assert s.request_validation(snap(45, bids=((100, "1"), (99, "6")))) == []
    assert s.last_validation is None  # noch nicht verglichen
    s.on_depth(upd(31, 50, 30, bids=((99, "6"),)))
    assert s.last_validation is not None
    assert s.last_validation.ok


def test_validation_skipped_when_history_too_short() -> None:
    s = new_sync(history=2)
    s.on_depth(upd(1, 10, 0))
    s.on_snapshot(snap(5))
    for i in range(1, 5):
        s.on_depth(upd(i * 10 + 1, i * 10 + 10, i * 10))
    s.request_validation(snap(12))
    assert s.last_validation is None
    assert s.stats.validations_skipped == 1


def test_validation_without_resync_flag_only_logs() -> None:
    s = new_sync(resync_on_mismatch=False)
    s.on_depth(upd(25, 30, 24))
    s.on_snapshot(snap(28))
    assert s.request_validation(snap(30, bids=((100, "9"),))) == []
    assert s.synced
    assert s.stats.validations_failed == 1
