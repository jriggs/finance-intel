"""
SQLite persistence for all app state.

Formerly a single ~1300-line ``db.py``; now a package split by domain — one
module per table-group — sharing a single thread-local connection pool
(:mod:`connection`) and schema (:mod:`schema`):

    watchlist · picks · symbols · crawl · sentiment · volume ·
    recommendations · screeners · insights · settings · rag_sources ·
    daily_trades

Everything is re-exported here so existing callers (``import db;
db.get_watchlist()``) keep working unchanged. The DB lives at
``app/data/finance.db`` by default (override with ``FINANCE_DB_PATH``); the
schema is created/migrated on import, in the importing thread.
"""
from __future__ import annotations

# ── Infrastructure ────────────────────────────────────────────────────────────
from .connection import (  # noqa: F401
    DB_PATH,
    _get_conn,
    _local,
    _write_lock,
    logger,
)
from .schema import (  # noqa: F401
    _bootstrap_avg_volumes,
    _init_schema,
    _migrate,
)
from .settings import (  # noqa: F401
    AppSettings,
    get_setting,
    set_setting,
)

# ── Domain repositories ───────────────────────────────────────────────────────
from .watchlist import (  # noqa: F401
    add_to_watchlist,
    get_watchlist,
    remove_from_watchlist,
)
from .picks import (  # noqa: F401
    _insert_pick,
    _row_to_pick,
    add_pick,
    close_pick,
    get_open_picks,
    get_pick_stats,
    get_picks,
    update_pick_prices,
)
from .symbols import (  # noqa: F401
    get_fetch_failed_symbols,
    get_symbol_count,
    get_symbol_names,
    get_symbols,
    mark_symbol_fetch_failed,
    save_symbol_avg_volumes,
    save_symbols,
)
from .crawl import (  # noqa: F401
    get_crawl_results,
    save_crawl_results,
)
from .sentiment import (  # noqa: F401
    get_sentiment,
    save_sentiment,
)
from .volume import (  # noqa: F401
    get_all_volume_results,
    get_all_volume_scores,
    get_all_volume_sentiments,
    get_data_ages,
    get_next_volume_task,
    get_next_volume_tasks,
    get_volume_result,
    get_volume_results,
    get_volume_task_counts,
    init_volume_analysis_tasks,
    mark_volume_tasks_for_rescore,
    save_volume_result,
    set_volume_task_cooldown,
    set_volume_task_priority,
    sync_volume_analysis_tasks,
    update_volume_task,
)
from .recommendations import (  # noqa: F401
    get_recommendations,
    save_recommendations,
)
from .screeners import (  # noqa: F401
    get_all_screener_results,
    get_screener_result,
    save_screener_result,
)
from .insights import (  # noqa: F401
    get_all_extended_insights,
    get_extended_insight,
    save_extended_insight,
)
from .rag_sources import (  # noqa: F401
    delete_rag_source,
    get_rag_sources,
    save_all_rag_sources,
    save_rag_source,
)
from .daily_trades import (  # noqa: F401
    close_daily_position,
    confirm_daily_session,
    flag_daily_position_sell,
    get_daily_history,
    get_daily_portfolio_stats,
    get_daily_positions_for_date,
    get_daily_session,
    get_open_daily_positions,
    save_daily_session,
    update_daily_position_prices,
)

# Initialise schema on import (runs in the importing thread), preserving the
# behavior of the former module-level bootstrap.
_init_schema()
_migrate()
_bootstrap_avg_volumes()
